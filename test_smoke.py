import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

import smoke


class SmokeRolloutTests(unittest.TestCase):
    def test_retries_against_the_staged_generation_and_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            deploy = root / ".state/deploy/_manja"
            deploy.mkdir(parents=True)
            (root / ".state/pending.json").write_text(json.dumps({"registry": {"generation": "new"}}))
            export = b'{"files": []}'
            (deploy / "export.json").write_bytes(export)
            with (
                patch.object(smoke, "ROOT", root),
                patch.object(smoke, "verify_publication", side_effect=[smoke.PublicationMismatch("older HTML"), None]) as verify,
                patch.object(smoke.time, "sleep") as sleep,
            ):
                smoke.main()
            self.assertEqual(verify.call_args_list, [call("new", export), call("new", export)])
            sleep.assert_called_once_with(1)

    def test_a_persistent_mismatch_still_fails(self):
        with (
            patch.object(smoke, "read_json", return_value=None),
            patch.object(smoke, "verify_publication", side_effect=smoke.PublicationMismatch("older HTML")) as verify,
            patch.object(smoke.time, "sleep"),
        ):
            with self.assertRaisesRegex(smoke.PublicationMismatch, "older HTML"):
                smoke.main()
            self.assertEqual(verify.call_count, 8)

    def test_rejects_a_previous_generation_before_checking_its_files(self):
        with patch.object(smoke, "fetch", return_value=b'{"generation": "old"}') as fetch:
            with self.assertRaisesRegex(smoke.PublicationMismatch, "expected generation"):
                smoke.verify_publication(expected_generation="new")
            fetch.assert_called_once_with("/publications.json")
