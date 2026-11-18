"""Tests for the read-only Linux diagnostics toolkit."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import diagnostics as d


class DiagnosticsTests(unittest.TestCase):
    def test_cpu_sample(self):
        with patch.object(d, 'read_text', return_value='cpu  10 2 3 80 5 1 2 3 0 0\n'):
            self.assertEqual(d.cpu_sample(), (106, 85))

    def test_cpu_usage(self):
        with patch.object(d, 'cpu_sample', side_effect=[(100, 70), (200, 130)]), \
                patch.object(d.time, 'sleep'), \
                patch.object(d.os, 'cpu_count', return_value=4), \
                patch.object(d.os, 'getloadavg', return_value=(0.1, 0.2, 0.3)):
            result = d.cpu_info()
        self.assertEqual(result['usage_percent'], 40.0)
        self.assertEqual(result['logical_cpus'], 4)
        self.assertEqual(result['load_average'], [0.1, 0.2, 0.3])

    def test_cpu_zero_delta(self):
        with patch.object(d, 'cpu_sample', return_value=(100, 70)), \
                patch.object(d.time, 'sleep'), \
                patch.object(d.os, 'getloadavg', return_value=(0, 0, 0)):
            self.assertIsNone(d.cpu_info()['usage_percent'])

    def test_memory_and_swap(self):
        sample = 'MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 200 kB\nSwapFree: 150 kB\n'
        with patch.object(d, 'read_text', return_value=sample):
            result = d.memory_info()
        self.assertEqual(result['ram_total_bytes'], 1000 * 1024)
        self.assertEqual(result['ram_used_percent'], 60.0)
        self.assertEqual(result['swap_used_bytes'], 50 * 1024)

    def test_memory_without_available(self):
        sample = 'MemTotal: 1000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n'
        with patch.object(d, 'read_text', return_value=sample):
            result = d.memory_info()
        self.assertIsNone(result['ram_used_percent'])
        self.assertEqual(result['swap_used_bytes'], 0)

    def test_disk_usage(self):
        with patch.object(d.shutil, 'disk_usage', return_value=(1000, 600, 400)):
            result = d.disk_info()
        self.assertEqual(result['used_percent'], 60.0)
        self.assertEqual(result['path'], '/')

    def test_process_sorting_and_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for pid, rss in [(10, 100), (20, 300), (30, 300)]:
                entry = root / str(pid)
                entry.mkdir()
                (entry / 'status').write_text(f'Name: worker\nVmRSS: {rss} kB\n')
            (root / '40').mkdir()  # Simulate a process disappearing or being unreadable.
            (root / 'not-a-process').mkdir()
            def fake_path(path):
                return root if str(path) == '/proc' else Path(path)
            with patch.object(d, 'Path', side_effect=fake_path):
                rows = d.process_info(2)
                empty = d.process_info(0)
        self.assertEqual([r['pid'] for r in rows], [20, 30])
        self.assertEqual(rows[0]['rss_bytes'], 300 * 1024)
        self.assertEqual(empty, [])

    def test_network_counters(self):
        sample = 'header\nheader\n eth0: 123 0 0 0 0 0 0 0 456 0 0 0 0 0 0 0\n'
        with patch.object(d, 'read_text', return_value=sample):
            self.assertEqual(d.network_info(), [{'interface': 'eth0', 'rx_bytes': 123, 'tx_bytes': 456}])

    def test_systemd_unavailable(self):
        with patch.object(d.shutil, 'which', return_value=None), \
                patch.object(d.subprocess, 'run') as run:
            self.assertFalse(d.service_info()['available'])
            run.assert_not_called()

    def test_systemd_parsing_and_read_only_command(self):
        stdout = 'demo.service loaded active running Example service\nother.service loaded inactive dead Other service\n'
        with patch.object(d.shutil, 'which', return_value='/usr/bin/systemctl'), \
                patch.object(d.Path, 'is_dir', return_value=True), \
                patch.object(d.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout, '')) as run:
            result = d.service_info()
        self.assertTrue(result['available'])
        self.assertEqual(result['units'][0], {'unit': 'demo.service', 'load': 'loaded', 'active': 'active', 'sub': 'running'})
        self.assertEqual(result['units'][1]['active'], 'inactive')
        self.assertEqual(run.call_args.args[0], ['/usr/bin/systemctl', 'list-units', '--type=service', '--all', '--plain', '--no-legend', '--no-pager'])
        self.assertEqual(run.call_args.kwargs['timeout'], 8)
        self.assertTrue(run.call_args.kwargs['check'])

    def test_collect_partial_failure(self):
        with patch.object(d, 'cpu_info', side_effect=OSError('unavailable')), \
                patch.object(d, 'memory_info', return_value={}), \
                patch.object(d, 'disk_info', return_value={}), \
                patch.object(d, 'process_info', return_value=[]), \
                patch.object(d, 'network_info', return_value=[]), \
                patch.object(d, 'service_info') as service:
            report = d.collect()
        self.assertEqual(report['cpu'], {'error': 'OSError'})
        self.assertIn('memory', report)
        self.assertNotIn('systemd', report)
        service.assert_not_called()

    def test_cli_compact_json(self):
        output = io.StringIO()
        with patch.object(d.platform, 'system', return_value='Linux'), \
                patch.object(d, 'collect', return_value={'version': d.VERSION}) as collect, \
                contextlib.redirect_stdout(output):
            status = d.main(['--top', '3', '--services', '--compact'])
        self.assertEqual(status, 0)
        collect.assert_called_once_with(3, True)
        self.assertEqual(json.loads(output.getvalue()), {'version': d.VERSION})

    def test_cli_error_exit_code(self):
        with patch.object(d.platform, 'system', return_value='Linux'), \
                patch.object(d, 'collect', return_value={'cpu': {'error': 'OSError'}}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.main([]), 1)

    def test_cli_invalid_limit(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for value in ['-1', '101']:
                with self.subTest(value=value), self.assertRaises(SystemExit) as caught:
                    d.main(['--top', value])
                self.assertEqual(caught.exception.code, 2)

    def test_cli_non_linux(self):
        with patch.object(d.platform, 'system', return_value='Windows'), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            d.main([])
        self.assertEqual(caught.exception.code, 1)


if __name__ == '__main__':
    unittest.main()
