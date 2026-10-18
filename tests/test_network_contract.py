"""Native Compose contract for the dedicated, unpublished metrics endpoint."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MetricsNetworkContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "fixture.env"
            env.write_text("METRICS_ENABLED=true\nMETRICS_HOST=0.0.0.0\nMETRICS_PORT=9102\n")
            result = subprocess.run(
                ["docker", "compose", "--env-file", str(env), "-f",
                 str(ROOT / "docker-compose.dokploy.yaml"), "config",
                 "--no-env-resolution", "--format", "json"],
                cwd=ROOT, check=True, capture_output=True, text=True,
            )
            cls.compose = json.loads(result.stdout)
        cls.service = cls.compose["services"]["connection-machine"]

    def test_no_host_publication(self):
        self.assertFalse(self.service.get("ports"))
        self.assertIn("9102", [str(value) for value in self.service.get("expose", [])])

    def test_preserves_default_and_adds_narrow_metrics_network(self):
        networks = self.service["networks"]
        self.assertIn("default", networks)
        self.assertIn("connection-machine-metrics", networks)
        self.assertIn("connection-machine-metrics", networks["connection-machine-metrics"]["aliases"])

    def test_metrics_network_is_explicit_external_dependency(self):
        network = self.compose["networks"]["connection-machine-metrics"]
        self.assertTrue(network["external"])
        self.assertEqual(network["name"], "connection-machine-metrics")

    def test_preserves_existing_data_volume_identity(self):
        volume = self.compose["volumes"]["connection-machine-data"]
        self.assertTrue(volume["external"])
        self.assertEqual(volume["name"], "connection-machine-data")
        self.assertEqual(self.service["volumes"][0]["target"], "/app/data")


if __name__ == "__main__":
    unittest.main()
