"""The locks pin every package the project asks for, so a rebuild never takes an untested version and never misses a
package that was added without running deploy/lock.sh."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)")


def requirements(path: Path) -> list[tuple[str, str, str]]:
    """(normalised name, version spec, marker) for each requirement line."""
    found = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        spec, _, marker = line.partition(";")
        name, rest = REQUIREMENT.fullmatch(spec.strip()).groups()
        found.append((re.sub(r"[-_.]+", "-", name).lower(), rest.strip(), marker.strip()))
    return found


class DependencyLockTests(unittest.TestCase):
    def check_lock(self, wanted: str, lock: str) -> dict[str, str]:
        pins = requirements(ROOT / lock)
        for name, spec, _ in pins:
            self.assertRegex(spec, r"^==[^=*,<>]+$", f"{lock}: {name} is not pinned to one version")
        missing = {name for name, _, _ in requirements(ROOT / wanted)} - {name for name, _, _ in pins}
        self.assertFalse(missing, f"{wanted} asks for {sorted(missing)}, missing from {lock}: run deploy/lock.sh")
        return {name: spec for name, spec, marker in pins if not marker or marker == "python_full_version >= '3.12'"}

    def test_image_lock_pins_every_requirement(self):
        self.check_lock("requirements.txt", "requirements.lock")

    def test_checks_use_the_image_versions(self):
        image = self.check_lock("requirements.txt", "requirements.lock")
        checks = self.check_lock("requirements-test.txt", "requirements-test.lock")
        differ = {name: (spec, image[name]) for name, spec in checks.items() if name in image and spec != image[name]}
        self.assertFalse(differ, f"the checks and the image pin different versions: {differ}; run deploy/lock.sh")


if __name__ == "__main__":
    unittest.main()
