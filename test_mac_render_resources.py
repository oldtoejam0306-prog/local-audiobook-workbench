import errno
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import mac_render_resources as resources


class FakeProcess:
    _pid = 4000

    def __init__(self):
        type(self)._pid += 1
        self.pid = type(self)._pid
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class MacRenderResourceTests(unittest.TestCase):
    def test_clonefile_only_and_preserves_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src, dst = root / "model", root / "clone"
            src.mkdir()
            (src / "weights").write_bytes(b"model bytes")
            calls = []

            def clonefile(a, b, flags):
                calls.append((os.fsdecode(a), os.fsdecode(b), flags))
                Path(os.fsdecode(b)).write_bytes(Path(os.fsdecode(a)).read_bytes())
                return 0

            fake_libc = types.SimpleNamespace(clonefile=clonefile)
            with mock.patch.object(resources.sys, "platform", "darwin"), \
                    mock.patch.object(resources.ctypes, "CDLL", return_value=fake_libc), \
                    mock.patch.object(resources.shutil, "copyfile", side_effect=AssertionError("copy fallback")):
                resources.clone_model_tree(src, dst)
            self.assertEqual((dst / "weights").read_bytes(), b"model bytes")
            self.assertNotEqual((src / "weights").stat().st_ino,
                                (dst / "weights").stat().st_ino)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][2], 0)

    def test_clone_failure_does_not_fall_back_to_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src, dst = root / "model", root / "clone"
            src.mkdir()
            (src / "weights").write_bytes(b"model bytes")
            fake_libc = types.SimpleNamespace(clonefile=lambda *_: -1)
            with mock.patch.object(resources.sys, "platform", "darwin"), \
                    mock.patch.object(resources.ctypes, "CDLL", return_value=fake_libc), \
                    mock.patch.object(resources.ctypes, "get_errno", return_value=errno.EIO), \
                    mock.patch.object(resources.shutil, "copyfile", side_effect=AssertionError("copy fallback")):
                with self.assertRaises(OSError):
                    resources.clone_model_tree(src, dst)

    @unittest.skipIf(os.name == "nt", "creating symlinks requires Windows developer mode")
    def test_clone_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "model"
            src.mkdir()
            (src / "link").symlink_to(src)
            with mock.patch.object(resources.sys, "platform", "darwin"):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    resources.clone_model_tree(src, root / "out")

    def test_clone_rejects_existing_destination(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "model"
            src.mkdir()
            with mock.patch.object(resources.sys, "platform", "darwin"):
                (root / "out").mkdir()
                with self.assertRaises(FileExistsError):
                    resources.clone_model_tree(src, root / "out")

    def test_mlx_limits(self):
        seen = []
        mx = types.SimpleNamespace(
            get_cache_limit=lambda: 1, get_memory_limit=lambda: 2, get_wired_limit=lambda: 3,
            set_cache_limit=lambda n: (seen.append(("cache", n)) or 11),
            set_memory_limit=lambda n: (seen.append(("memory", n)) or 22),
            set_wired_limit=lambda n: (seen.append(("wired", n)) or 33))
        mlx = types.ModuleType("mlx")
        mlx.core = mx
        with mock.patch.dict(sys.modules, {"mlx": mlx, "mlx.core": mx}):
            result = resources.configure_mlx_limits()
        self.assertEqual(result["limits"], {"cache_limit": 256 * 1024**2,
                                            "memory_limit": 6 * resources.GIB,
                                            "wired_limit": 0})
        self.assertEqual(result["previous"], {"cache_limit": 11, "memory_limit": 22, "wired_limit": 33})
        self.assertEqual([name for name, _ in seen], ["cache", "memory", "wired"])
        self.assertTrue(result["memory_limit_is_guideline"])

    def test_render_lock_competition_and_release(self):
        held = set()
        fake_fcntl = types.ModuleType("fcntl")
        fake_fcntl.LOCK_EX, fake_fcntl.LOCK_NB, fake_fcntl.LOCK_UN = 1, 2, 4

        def flock(fd, op):
            import os
            key = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            if op == fake_fcntl.LOCK_EX | fake_fcntl.LOCK_NB:
                if key in held:
                    raise BlockingIOError(errno.EAGAIN, "busy")
                held.add(key)
            elif op == fake_fcntl.LOCK_UN:
                held.discard(key)

        fake_fcntl.flock = flock
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(sys.modules, {"fcntl": fake_fcntl}), \
                mock.patch.object(resources.shutil, "disk_usage", return_value=types.SimpleNamespace(free=17 * resources.GIB)), \
                mock.patch.object(resources, "_free_memory_percent", return_value=60), \
                mock.patch.object(resources.subprocess, "Popen", side_effect=lambda *a, **k: FakeProcess()):
            with resources.render_slot(td) as config:
                self.assertEqual(config["free_memory_percent"], 60)
                self.assertIsNotNone(config["caffeinate_pid"])
                with self.assertRaisesRegex(RuntimeError, "another TTS render"):
                    with resources.render_slot(td):
                        pass
            with resources.render_slot(td):
                pass
            self.assertTrue((Path(td) / ".tts-resource.lock").exists())
            self.assertFalse(held)

    def test_render_preflight_refusal_releases_lock(self):
        held = set()
        fake_fcntl = types.ModuleType("fcntl")
        fake_fcntl.LOCK_EX, fake_fcntl.LOCK_NB, fake_fcntl.LOCK_UN = 1, 2, 4
        def flock(fd, op):
            import os
            key = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            held.add(key) if op == 3 else held.discard(key)
        fake_fcntl.flock = flock
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(sys.modules, {"fcntl": fake_fcntl}), \
                mock.patch.object(resources.shutil, "disk_usage", return_value=types.SimpleNamespace(free=1)), \
                mock.patch.object(resources, "_free_memory_percent", return_value=60), \
                mock.patch.object(resources.subprocess, "Popen", side_effect=lambda *a, **k: FakeProcess()):
            with self.assertRaisesRegex(RuntimeError, "free disk"):
                with resources.render_slot(td):
                    pass
            self.assertFalse(held)

    def test_render_exception_cleans_up_wakeguard(self):
        held = set()
        fake_fcntl = types.ModuleType("fcntl")
        fake_fcntl.LOCK_EX, fake_fcntl.LOCK_NB, fake_fcntl.LOCK_UN = 1, 2, 4

        def flock(fd, op):
            import os
            key = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            held.add(key) if op == 3 else held.discard(key)
        fake_fcntl.flock = flock
        process = FakeProcess()
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(sys.modules, {"fcntl": fake_fcntl}), \
                mock.patch.object(resources.shutil, "disk_usage", return_value=types.SimpleNamespace(free=17 * resources.GIB)), \
                mock.patch.object(resources, "_free_memory_percent", return_value=60), \
                mock.patch.object(resources.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "render failed"):
                with resources.render_slot(td):
                    raise ValueError("render failed")
            self.assertTrue(process.terminated)
            self.assertFalse(process.killed)
            self.assertFalse(held)


if __name__ == "__main__":
    unittest.main()
