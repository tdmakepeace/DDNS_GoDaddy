"""Config and hostname splitting for the 123DNS GoDaddy agent."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent import ConfigError, load_config, split_record_name


class SplitRecordNameTests(unittest.TestCase):
    def test_gateway_label(self) -> None:
        self.assertEqual(split_record_name("gateway", "example.com"), "gateway")

    def test_gateway_fqdn(self) -> None:
        self.assertEqual(
            split_record_name("gateway.example.com", "example.com"), "gateway"
        )

    def test_apex(self) -> None:
        self.assertEqual(split_record_name("example.com", "example.com"), "@")
        self.assertEqual(split_record_name("@", "example.com"), "@")

    def test_uk_sld(self) -> None:
        self.assertEqual(
            split_record_name("gateway.example.co.uk", "example.co.uk"), "gateway"
        )

    def test_mismatch_raises(self) -> None:
        with self.assertRaises(ConfigError):
            split_record_name("vpn.other.com", "example.com")


class LoadConfigTests(unittest.TestCase):
    def test_nested_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "a_record: gateway",
                        "domain: example.co.uk",
                        "keys:",
                        "  api_key: key-one",
                        "  api_secret: secret-one",
                    ]
                ),
                encoding="utf-8",
            )
            settings = load_config(path)
            self.assertEqual(len(settings.records), 1)
            self.assertEqual(settings.records[0].name, "gateway")
            self.assertEqual(settings.records[0].a_record, "gateway.example.co.uk")
            self.assertEqual(settings.api_key, "key-one")
            self.assertEqual(settings.ttl, 600)

    def test_multiple_a_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "a_records:",
                        "  - gateway",
                        "  - vpn",
                        "  - '@'",
                        "domain: example.co.uk",
                        "keys:",
                        "  api_key: key-one",
                        "  api_secret: secret-one",
                    ]
                ),
                encoding="utf-8",
            )
            settings = load_config(path)
            self.assertEqual(
                [(record.name, record.a_record) for record in settings.records],
                [
                    ("gateway", "gateway.example.co.uk"),
                    ("vpn", "vpn.example.co.uk"),
                    ("@", "example.co.uk"),
                ],
            )

    def test_dedupes_label_and_fqdn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "a_records:",
                        "  - gateway",
                        "  - gateway.example.com",
                        "a_record: vpn",
                        "domain: example.com",
                        "keys:",
                        "  api_key: key-one",
                        "  api_secret: secret-one",
                    ]
                ),
                encoding="utf-8",
            )
            settings = load_config(path)
            self.assertEqual(
                [record.name for record in settings.records],
                ["gateway", "vpn"],
            )

    def test_rejects_empty_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "domain: example.com",
                        "keys:",
                        "  api_key: key-one",
                        "  api_secret: secret-one",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_top_level_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "a_record: gateway",
                        "domain: example.com",
                        "api_key: key-two",
                        "api_secret: secret-two",
                    ]
                ),
                encoding="utf-8",
            )
            settings = load_config(path)
            self.assertEqual(settings.api_secret, "secret-two")

    def test_rejects_placeholder_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "a_record: gateway",
                        "domain: example.com",
                        "keys:",
                        "  api_key: CHANGE_ME_API_KEY",
                        "  api_secret: CHANGE_ME_API_SECRET",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
