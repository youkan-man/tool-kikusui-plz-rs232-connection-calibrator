import os
import threading
import unittest

from plz_rs232_diag.diagnostics import (
    ICOUNT_FIELDS,
    classify_probe,
    count_delta,
    duplicate_usb_exposures,
    probe_port,
    response_record,
    serial,
)


class ResponseTests(unittest.TestCase):
    def test_valid_kikusui_idn(self):
        row = response_record(b"KIKUSUI,PLZ-50F,0,1.09\n")

        self.assertTrue(row["valid_kikusui_idn"])
        self.assertEqual(23, row["length"])
        self.assertEqual(1.0, row["printable_ratio"])

    def test_binary_response_is_preserved(self):
        row = response_record(bytes.fromhex("5a5bda00"))

        self.assertFalse(row["valid_kikusui_idn"])
        self.assertEqual("5a5bda00", row["hex"])
        self.assertIn("\\xda", row["ascii"])

    @unittest.skipUnless(os.name == "posix" and serial is not None, "requires POSIX and pyserial")
    def test_probe_over_pseudo_terminal(self):
        master_fd, slave_fd = os.openpty()
        slave_name = os.ttyname(slave_fd)
        expected_query = b"*IDN?\n"
        response = b"KIKUSUI,PLZ-50F,0,1.09\n"

        def responder():
            buffered = b""
            replies = 0
            while replies < 2:
                buffered += os.read(master_fd, 128)
                while expected_query in buffered and replies < 2:
                    before, buffered = buffered.split(expected_query, 1)
                    self.assertEqual(b"", before)
                    os.write(master_fd, response)
                    replies += 1

        worker = threading.Thread(target=responder, daemon=True)
        worker.start()
        try:
            result = probe_port(
                slave_name,
                repeats=2,
                timeout=0.2,
                collect_counts=False,
            )
            worker.join(timeout=1)
        finally:
            os.close(slave_fd)
            os.close(master_fd)

        self.assertFalse(worker.is_alive())
        self.assertEqual("reachable", result["status"])
        self.assertEqual([True, True], [row["valid_kikusui_idn"] for row in result["responses"]])


class CounterTests(unittest.TestCase):
    def test_count_delta(self):
        before = {name: 10 for name in ICOUNT_FIELDS}
        after = {name: 12 for name in ICOUNT_FIELDS}

        self.assertEqual({name: 2 for name in ICOUNT_FIELDS}, count_delta(before, after))

    def test_corrupt_reply_with_errors(self):
        responses = [response_record(b"garbage\x00")]
        counters = {"delta": {"frame": 1, "brk": 1}}

        self.assertEqual("reply-with-uart-errors", classify_probe(responses, counters))

    def test_tx_without_rx(self):
        responses = [response_record(b"")]
        counters = {"delta": {"tx": 6, "rx": 0}}

        self.assertEqual("transmitted-no-rx", classify_probe(responses, counters))


class UsbTests(unittest.TestCase):
    def test_configured_and_unconfigured_identity_is_reported(self):
        devices = [
            {
                "node": "1-6",
                "vid": "0403",
                "pid": "6001",
                "serial": "ABC",
                "product": "FT232R USB UART",
                "configuration": "1",
            },
            {
                "node": "1-8.3",
                "vid": "0403",
                "pid": "6001",
                "serial": "ABC",
                "product": "FT232R USB UART",
                "configuration": "",
            },
        ]

        rows = duplicate_usb_exposures(devices)

        self.assertEqual(1, len(rows))
        self.assertEqual(["1-6"], rows[0]["configured_nodes"])
        self.assertEqual(["1-8.3"], rows[0]["unconfigured_nodes"])


if __name__ == "__main__":
    unittest.main()
