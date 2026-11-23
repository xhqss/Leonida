"""Unit tests for tools/gallery.py standalone 3D Studio & Asset Gallery server."""
import json
import io
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.parse
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS_DIR = os.path.join(ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import gallery


def _make_1x1_rgba_dds() -> bytes:
    """Build a minimal 1x1 uncompressed RGBA DDS image for synthetic .dds -> .png preview tests."""
    hdr = bytearray(128)
    hdr[0:4] = b"DDS "
    struct.pack_into("<I", hdr, 4, 124)
    struct.pack_into("<III", hdr, 8, 0x1007, 1, 1)
    struct.pack_into("<I", hdr, 76, 32)
    struct.pack_into("<I", hdr, 80, 0x41)  # DDPF_RGB | DDPF_ALPHAPIXELS
    struct.pack_into("<IIIII", hdr, 88, 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    return bytes(hdr) + b"\x11\x22\x33\xff"


def _raw_get(port: int, target: str, host: str | None = "127.0.0.1", extra_headers: tuple[str, ...] = ()):
    """Send a raw HTTP/1.1 GET without client-side path normalization or header merging."""
    lines = [f"GET {target} HTTP/1.1"]
    if host is not None:
        lines.append(f"Host: {host}")
    lines.extend(extra_headers)
    lines.extend(["Connection: close", "", ""])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall("\r\n".join(lines).encode("latin-1"))
        chunks = []
        while data := sock.recv(65536):
            chunks.append(data)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, head.decode("latin-1"), body


class TestGalleryCategorization(unittest.TestCase):
    def test_categories(self) -> None:
        self.assertEqual(gallery.categorize_model("ped_lucia_body.glb"), "Персонажи (Characters)")
        self.assertEqual(gallery.categorize_model("veh_infernus.glb"), "Транспорт (Vehicles)")
        self.assertEqual(gallery.categorize_model("w_rifle_m4.glb"), "Оружие (Weapons)")
        self.assertEqual(gallery.categorize_model("build_wall_01.glb"), "Карта и Здания (World)")
        self.assertEqual(gallery.categorize_model("prop_chair_office.glb"), "Объекты и Пропы (Props)")
        self.assertEqual(gallery.categorize_model("tree_palm_02.glb"), "Растительность (Nature)")
        self.assertEqual(gallery.categorize_model("misc_something.glb"), "Прочее (Miscellaneous)")

    def test_folder_inference(self) -> None:
        self.assertEqual(gallery.categorize_model("asset_01.glb", "clean_vehicles"), "Транспорт (Vehicles)")
        self.assertEqual(gallery.categorize_model("model.glb", "unpacked_peds"), "Персонажи (Characters)")


class TestHostAllowed(unittest.TestCase):
    def test_loopback_and_missing_hosts_are_allowed(self) -> None:
        for host in (None, "127.0.0.1", "127.0.0.1:8080", "localhost", "LOCALHOST:8080",
                     "localhost.", "localhost.:8080", "[::1]", "[::1]:8080", " localhost "):
            with self.subTest(host=host):
                self.assertTrue(gallery.host_allowed(host))

    def test_foreign_and_malformed_hosts_are_refused(self) -> None:
        for host in ("", "evil.example", "evil.example:8080", "127.0.0.1.evil.example",
                     "localhost.evil.example:8080", "0.0.0.0:8080", "::1",
                     "localhost:", "localhost:abc", "localhost:65536", "localhost:" + "9" * 5000,
                     "[::1]junk", "[::1].", "[::1]:abc", "[::1]:8080:1", "127.0.0.1:80:80"):
            with self.subTest(host=host[:40]):
                self.assertFalse(gallery.host_allowed(host))


class TestSafeSubpath(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "assets")
        self.sibling = os.path.join(self.tmp.name, "assets_secret")
        os.makedirs(os.path.join(self.base, "sub"), exist_ok=True)
        os.makedirs(self.sibling, exist_ok=True)
        with open(os.path.join(self.base, "ok.glb"), "wb") as f:
            f.write(b"ok")
        with open(os.path.join(self.base, "sub", "nested.glb"), "wb") as f:
            f.write(b"nested")
        with open(os.path.join(self.sibling, "loot.glb"), "wb") as f:
            f.write(b"loot")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_valid_paths_resolve_inside_base(self) -> None:
        for rel in ("ok.glb", "sub/nested.glb", "COM10.glb", "my.CON"):
            with self.subTest(rel=rel):
                resolved = gallery.safe_subpath(self.base, rel)
                self.assertIsNotNone(resolved)
                self.assertEqual(
                    os.path.normcase(os.path.commonpath([os.path.realpath(self.base), resolved])),
                    os.path.normcase(os.path.realpath(self.base)),
                )

    def test_traversal_and_invalid_segments_are_refused(self) -> None:
        for rel in ("", "/", "\\", ".", "..", "sub", "sub/", "/ok.glb", "ok.glb/",
                    "../assets_secret/loot.glb", "..\\assets_secret\\loot.glb",
                    "sub/../../assets_secret/loot.glb", "sub//nested.glb", "./ok.glb",
                    "ok\x00.glb", "C:ok.glb", "ok.glb:stream"):
            with self.subTest(rel=rel):
                self.assertIsNone(gallery.safe_subpath(self.base, rel))

    def test_windows_device_names_refused_before_realpath(self) -> None:
        names = ("CON", "con.glb", "NUL", "NUL .glb", "COM1", "COM\u00b9.glb", "LPT\u00b2.png", "CONIN$", "conout$.bin")
        with mock.patch.object(gallery.os.path, "isreserved", None, create=True):
            with mock.patch.object(gallery.os.path, "realpath",
                                   side_effect=AssertionError("device path reached realpath")) as rp:
                for name in names:
                    with self.subTest(name=name):
                        self.assertIsNone(gallery.safe_subpath(self.base, name))
                        self.assertIsNone(gallery.safe_subpath(self.base, f"sub/{name}"))
                rp.assert_not_called()

    def test_symlink_escape_is_refused(self) -> None:
        link = os.path.join(self.base, "escape.glb")
        try:
            os.symlink(os.path.join(self.sibling, "loot.glb"), link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot create symlink here: {exc}")
        self.assertIsNone(gallery.safe_subpath(self.base, "escape.glb"))

    def test_realpath_escape_is_refused(self) -> None:
        outside = os.path.realpath(os.path.join(self.sibling, "loot.glb"))
        real_base = os.path.realpath(self.base)
        with mock.patch.object(gallery.os.path, "realpath",
                               side_effect=lambda p: real_base if p == self.base else outside):
            self.assertIsNone(gallery.safe_subpath(self.base, "escape.glb"))


class TestGalleryServer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.models_dir = os.path.join(self.tmp.name, "models")
        self.models_secret = os.path.join(self.tmp.name, "models_secret")
        self.textures_dir = os.path.join(self.tmp.name, "textures")
        self.textures_secret = os.path.join(self.tmp.name, "textures_secret")
        self.viewer_dir = os.path.join(self.tmp.name, "viewer")
        self.viewer_secret = os.path.join(self.tmp.name, "viewer_secret")
        self.vendor_dir = os.path.join(self.viewer_dir, "vendor")
        for d in (self.models_dir, self.models_secret, self.textures_dir,
                  self.textures_secret, self.viewer_dir, self.viewer_secret, self.vendor_dir):
            os.makedirs(d, exist_ok=True)

        # Create dummy assets
        with open(os.path.join(self.models_dir, "test_car.glb"), "wb") as f:
            f.write(b"glTF\x02\x00\x00\x00\x0c\x00\x00\x00")

        with open(os.path.join(self.textures_dir, "test_tex.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

        with open(os.path.join(self.textures_dir, "inside.dds"), "wb") as f:
            f.write(_make_1x1_rgba_dds())

        with open(os.path.join(self.viewer_dir, "studio.html"), "w", encoding="utf-8", newline="\n") as f:
            f.write("<!DOCTYPE html><html><body><h1>Studio</h1></body></html>")

        with open(os.path.join(self.viewer_dir, "app.js"), "w", encoding="utf-8", newline="\n") as f:
            f.write("console.log('ok');\n")

        with open(os.path.join(self.vendor_dir, "three.min.js"), "w", encoding="utf-8", newline="\n") as f:
            f.write("// three\n")

        # Files outside the served roots that must never be served
        with open(os.path.join(self.tmp.name, "secret.txt"), "wb") as f:
            f.write(b"TOP_SECRET")
        with open(os.path.join(self.tmp.name, "outside.dds"), "wb") as f:
            f.write(_make_1x1_rgba_dds())
        with open(os.path.join(self.models_secret, "loot.glb"), "wb") as f:
            f.write(b"LOOT_GLB")
        with open(os.path.join(self.textures_secret, "loot.png"), "wb") as f:
            f.write(b"LOOT_PNG")
        with open(os.path.join(self.viewer_secret, "loot.txt"), "wb") as f:
            f.write(b"LOOT_VIEWER")

        # Configure handler
        gallery.StudioHandler.models_dir = self.models_dir
        gallery.StudioHandler.textures_dir = self.textures_dir
        gallery.StudioHandler.viewer_dir = self.viewer_dir

        # Start server on an open port
        addr = ("127.0.0.1", 0)
        self.httpd = gallery.socketserver.TCPServer(addr, gallery.StudioHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.1)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_endpoints(self) -> None:
        base = f"http://127.0.0.1:{self.port}"

        # 1. Studio HTML root
        with urllib.request.urlopen(f"{base}/", timeout=3) as r:
            self.assertEqual(r.status, 200)
            body = r.read().decode("utf-8")
            self.assertIn("<h1>Studio</h1>", body)

        # 2. Models API
        with urllib.request.urlopen(f"{base}/api/models", timeout=3) as r:
            self.assertEqual(r.status, 200)
            data = json.loads(r.read())
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["models"][0]["name"], "test_car.glb")
            self.assertEqual(data["models"][0]["category"], "Транспорт (Vehicles)")

        # 3. Textures API
        with urllib.request.urlopen(f"{base}/api/textures", timeout=3) as r:
            self.assertEqual(r.status, 200)
            tdata = json.loads(r.read())
            self.assertEqual(tdata["count"], 2)
            self.assertEqual([t["name"] for t in tdata["textures"]], ["inside.dds", "test_tex.png"])

        # 4. Binary serving & DDS preview fallback inside textures_dir
        with urllib.request.urlopen(f"{base}/models/test_car.glb", timeout=3) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(r.read().startswith(b"glTF"))

        with urllib.request.urlopen(f"{base}/textures/test_tex.png", timeout=3) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(r.read().startswith(b"\x89PNG"))

        with urllib.request.urlopen(f"{base}/textures/inside.png", timeout=3) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(r.read().startswith(b"\x89PNG"))

        # 5. Viewer static & vendor assets
        with urllib.request.urlopen(f"{base}/vendor/three.min.js", timeout=3) as r:
            self.assertEqual(r.status, 200)
            self.assertIn(b"// three", r.read())

        with urllib.request.urlopen(f"{base}/app.js", timeout=3) as r:
            self.assertEqual(r.status, 200)
            self.assertIn(b"console.log", r.read())

    def test_traversal_and_sibling_prefix_escapes_are_forbidden(self) -> None:
        host = f"127.0.0.1:{self.port}"
        cases = (
            ("/vendor/../../secret.txt", b"TOP_SECRET"),
            ("/vendor/%2e%2e/%2e%2e/secret.txt", b"TOP_SECRET"),
            ("/models/../models_secret/loot.glb", b"LOOT_GLB"),
            ("/models/..%5cmodels_secret%5cloot.glb", b"LOOT_GLB"),
            ("/textures/../textures_secret/loot.png", b"LOOT_PNG"),
            ("/textures/../outside.png", b"\x89PNG"),
            ("/textures/%2e%2e/outside.png", b"\x89PNG"),
            ("/../viewer_secret/loot.txt", b"LOOT_VIEWER"),
            ("/%2e%2e/viewer_secret/loot.txt", b"LOOT_VIEWER"),
            ("/%2fviewer_secret/loot.txt", b"LOOT_VIEWER"),
            ("/models//test_car.glb", b"glTF"),
        )
        for target, secret in cases:
            with self.subTest(target=target):
                status, _, body = _raw_get(self.port, target, host=host)
                self.assertEqual(status, 403)
                self.assertNotIn(secret, body)

    def test_dds_symlink_escape_is_forbidden(self) -> None:
        link = os.path.join(self.textures_dir, "escaped.dds")
        try:
            os.symlink(os.path.join(self.tmp.name, "outside.dds"), link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot create symlink here: {exc}")
        status, _, body = _raw_get(self.port, "/textures/escaped.png", host=f"127.0.0.1:{self.port}")
        self.assertEqual(status, 403)
        self.assertNotIn(b"\x89PNG", body)

    def test_host_header_guards(self) -> None:
        loopback = f"127.0.0.1:{self.port}"
        for target in ("/", "/api/models", "/api/textures", "/models/test_car.glb", "/textures/test_tex.png"):
            with self.subTest(case="foreign", target=target):
                status, _, body = _raw_get(self.port, target, host="evil.example:8080")
                self.assertEqual(status, 403)
                self.assertNotIn(b"test_car.glb", body)
                self.assertNotIn(b"glTF", body)
            with self.subTest(case="duplicate", target=target):
                status, _, body = _raw_get(self.port, target, host=loopback, extra_headers=("Host: evil.example",))
                self.assertEqual(status, 400)
                self.assertNotIn(b"test_car.glb", body)
                self.assertNotIn(b"glTF", body)

        status, _, body = _raw_get(self.port, "/models/test_car.glb", host=None)
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"glTF"))

    def test_windows_device_names_get_403_without_opening(self) -> None:
        host = f"127.0.0.1:{self.port}"
        targets = ("/models/COM%C2%B9.glb", "/textures/NUL%20.png", "/vendor/CONIN%24", "/AUX.js")
        with mock.patch.object(gallery.os.path, "isreserved", None, create=True):
            with mock.patch("builtins.open", side_effect=AssertionError("device path reached open")) as opened:
                for target in targets:
                    with self.subTest(target=target):
                        status, _, _ = _raw_get(self.port, target, host=host)
                        self.assertEqual(status, 403)
                opened.assert_not_called()

    def test_missing_valid_paths_return_404(self) -> None:
        host = f"127.0.0.1:{self.port}"
        for target in ("/vendor/missing.js", "/models/missing.glb", "/textures/missing.png", "/missing.js"):
            with self.subTest(target=target):
                status, _, _ = _raw_get(self.port, target, host=host)
                self.assertEqual(status, 404)


class TestGalleryCommandLine(unittest.TestCase):
    def test_rejects_out_of_range_ports_before_starting_server(self) -> None:
        for port in (-1, 65536):
            with (
                self.subTest(port=port),
                mock.patch.object(gallery, "start_server") as start_server,
                mock.patch.object(sys, "stderr", new_callable=io.StringIO) as stderr,
            ):
                with self.assertRaises(SystemExit) as raised:
                    gallery.main(["--port", str(port), "--no-browser"])

                self.assertEqual(raised.exception.code, 2)
                self.assertIn("--port must be between 0 and 65535", stderr.getvalue())
                start_server.assert_not_called()

    def test_passes_default_and_boundary_ports_to_server(self) -> None:
        for args, port in (([], 8080), (["--port", "0"], 0), (["--port", "65535"], 65535)):
            with self.subTest(port=port), mock.patch.object(gallery, "start_server") as start_server:
                self.assertEqual(gallery.main([*args, "--no-browser"]), 0)
                start_server.assert_called_once_with(
                    port=port, models_dir=None, textures_dir=None, open_browser=False,
                    retry_next_port=not args,
                )

    def test_reports_unavailable_listen_ports(self) -> None:
        cases = ((["--port", "65535"], [65535]), (["--port", "8080"], [8080]), ([], [8080, 8081]))
        for args, ports in cases:
            with (
                self.subTest(ports=ports),
                tempfile.TemporaryDirectory() as assets,
                mock.patch.object(gallery.http.server.HTTPServer, "__init__", side_effect=OSError("address in use")) as bind,
                mock.patch.object(sys, "stderr", new_callable=io.StringIO) as stderr,
            ):
                with self.assertRaises(SystemExit) as raised:
                    gallery.main([*args, "--models-dir", assets, "--textures-dir", assets, "--no-browser"])

                self.assertEqual(raised.exception.code, 2)
                self.assertIn(f"can't listen on port {ports[-1]}", stderr.getvalue())
                self.assertEqual([attempt.args[0] for attempt in bind.call_args_list],
                                 [("127.0.0.1", port) for port in ports])

    def test_implicit_port_can_use_the_next_port(self) -> None:
        with (
            tempfile.TemporaryDirectory() as assets,
            mock.patch.object(gallery.http.server.HTTPServer, "__init__",
                              side_effect=[OSError("address in use"), None]) as bind,
            mock.patch.object(gallery.http.server.HTTPServer, "server_port", 8081, create=True),
            mock.patch.object(gallery.http.server.HTTPServer, "serve_forever") as serve,
            mock.patch.object(sys, "stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(gallery.main(["--models-dir", assets, "--textures-dir", assets, "--no-browser"]), 0)
            self.assertEqual([attempt.args[0] for attempt in bind.call_args_list],
                             [("127.0.0.1", 8080), ("127.0.0.1", 8081)])
            self.assertIn("http://localhost:8081", stdout.getvalue())
            serve.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
