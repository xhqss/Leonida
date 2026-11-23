"""Standalone local 3D Web Studio & Texture Gallery server for Leonida.

Launches a lightweight local HTTP server and opens a modern WebGL/glTF
3D asset inspector in the browser with Vice City / Leonida aesthetic.

Usage:
    python -I tools/gallery.py [--port 8080] [--models-dir <path>] [--textures-dir <path>]
"""
from __future__ import annotations

import argparse
import http.server
import json
import mimetypes
import os
import socketserver
import sys
import urllib.parse
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import dds_png
except ImportError:
    dds_png = None


def categorize_model(name: str, folder: str = "") -> str:
    combined = f"{folder}/{name}".lower()
    if any(k in combined for k in ("veh", "car", "truck", "bike", "wheel", "heli", "boat", "chassis", "motor", "engine")):
        return "Транспорт (Vehicles)"
    elif any(k in combined for k in ("weapon", "gun", "pistol", "rifle", "knife", "ammo", "shotgun", "smg")):
        return "Оружие (Weapons)"
    elif any(k in combined for k in ("light", "lamp", "sign", "box", "chair", "table", "prop", "debris", "anm", "amb", "core")):
        return "Объекты и Пропы (Props)"
    elif any(k in combined for k in ("tree", "grass", "plant", "bush", "flower", "water", "palm")):
        return "Растительность (Nature)"
    elif any(k in combined for k in ("build", "structure", "house", "bridge", "farlod", "station", "shop", "wall", "interior", "level", "road", "terrain")):
        return "Карта и Здания (World)"
    elif any(k in combined for k in ("ped", "female", "male", "head", "body", "lucia", "jason", "jacket", "face", "cloth", "skin", "_hair", "hair_")):
        return "Персонажи (Characters)"
    return "Прочее (Miscellaneous)"


class StudioHandler(http.server.BaseHTTPRequestHandler):
    models_dir: str = ""
    textures_dir: str = ""
    viewer_dir: str = ""

    def log_message(self, format: str, *args: object) -> None:
        pass  # Quiet logging

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path in ("/", "/index.html", "/studio.html"):
            studio_path = os.path.join(self.viewer_dir, "studio.html")
            index_path = os.path.join(self.viewer_dir, "index.html")
            target = studio_path if os.path.isfile(studio_path) else index_path
            if os.path.isfile(target):
                self._send_file(target, "text/html; charset=utf-8")
            else:
                self.send_error(404, "Viewer page not found")
            return

        if path.startswith("/vendor/"):
            rel = path[len("/vendor/"):].lstrip("/")
            vpath = os.path.normpath(os.path.join(self.viewer_dir, "vendor", rel))
            if os.path.isfile(vpath):
                mime = mimetypes.guess_type(vpath)[0] or "application/javascript"
                self._send_file(vpath, mime)
            else:
                self.send_error(404, "Vendor file not found")
            return

        # Serve static assets from viewer directory (e.g. style.css, app.js)
        rel_viewer = path.lstrip("/")
        vfile = os.path.normpath(os.path.join(self.viewer_dir, rel_viewer))
        if os.path.isfile(vfile) and vfile.startswith(self.viewer_dir):
            mime = mimetypes.guess_type(vfile)[0] or "application/octet-stream"
            if mime.startswith("text/"):
                mime += "; charset=utf-8"
            self._send_file(vfile, mime)
            return

        if path == "/api/models":
            self._serve_models_json()
            return

        if path == "/api/textures":
            self._serve_textures_json()
            return

        if path.startswith("/models/"):
            rel = path[len("/models/"):].lstrip("/")
            mpath = os.path.normpath(os.path.join(self.models_dir, rel))
            # Security: ensure mpath is inside models_dir
            if os.path.isfile(mpath) and mpath.startswith(self.models_dir):
                self._send_file(mpath, "model/gltf-binary")
            else:
                self.send_error(404, "Model not found")
            return

        if path.startswith("/textures/"):
            rel = path[len("/textures/"):].lstrip("/")
            tpath = os.path.normpath(os.path.join(self.textures_dir, rel))

            if not os.path.isfile(tpath) and rel.lower().endswith(".png"):
                # Try matching .dds file if png requested
                stem = os.path.splitext(rel)[0]
                dds_cand = os.path.normpath(os.path.join(self.textures_dir, stem + ".dds"))
                if os.path.isfile(dds_cand) and dds_png:
                    try:
                        png_bytes = dds_png.dds_file_to_png_bytes(dds_cand, max_size=1024)
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Content-Length", str(len(png_bytes)))
                        self.send_header("Cache-Control", "max-age=86400")
                        self.end_headers()
                        self.wfile.write(png_bytes)
                        return
                    except Exception:
                        pass

            if os.path.isfile(tpath) and tpath.startswith(self.textures_dir):
                mime = mimetypes.guess_type(tpath)[0] or "application/octet-stream"
                self._send_file(tpath, mime)
            else:
                self.send_error(404, "Texture not found")
            return

        self.send_error(404, "Not Found")

    def _send_file(self, file_path: str, content_type: str) -> None:
        try:
            size = os.path.getsize(file_path)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):
                    self.wfile.write(chunk)
        except Exception as exc:
            self.send_error(500, str(exc))

    def _serve_models_json(self) -> None:
        items = []
        if os.path.isdir(self.models_dir):
            for root, dirs, files in os.walk(self.models_dir):
                dirs.sort()
                for fn in sorted(files):
                    if fn.lower().endswith((".glb", ".gltf")):
                        full_path = os.path.join(root, fn)
                        rel_path = os.path.relpath(full_path, self.models_dir).replace("\\", "/")
                        rel_dir = os.path.relpath(root, self.models_dir).replace("\\", "/")
                        folder_name = "Корневая папка" if rel_dir == "." else rel_dir
                        try:
                            fsize = os.path.getsize(full_path)
                        except OSError:
                            fsize = 0
                        items.append({
                            "name": fn,
                            "path": rel_path,
                            "folder": folder_name,
                            "category": categorize_model(fn, folder_name),
                            "size": fsize,
                            "url": f"/models/{urllib.parse.quote(rel_path)}",
                        })

        # Group by category and folders for the UI
        categories = {}
        folders = {}
        for item in items:
            cat = item["category"]
            fld = item["folder"]
            categories.setdefault(cat, []).append(item)
            folders.setdefault(fld, []).append(item)

        payload = json.dumps({
            "count": len(items),
            "models": items,
            "categories": {k: len(v) for k, v in categories.items()},
            "folders": {k: len(v) for k, v in folders.items()},
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_textures_json(self) -> None:
        items = []
        if os.path.isdir(self.textures_dir):
            for root, dirs, files in os.walk(self.textures_dir):
                dirs.sort()
                for fn in sorted(files):
                    lower = fn.lower()
                    if lower.endswith((".dds", ".png", ".jpg", ".webp")):
                        full_path = os.path.join(root, fn)
                        rel_path = os.path.relpath(full_path, self.textures_dir).replace("\\", "/")
                        try:
                            fsize = os.path.getsize(full_path)
                        except OSError:
                            fsize = 0
                        fmt = "DirectDraw Surface (DDS)" if lower.endswith(".dds") else "PNG Image"
                        preview_rel = os.path.splitext(rel_path)[0] + ".png" if lower.endswith(".dds") else rel_path
                        items.append({
                            "name": fn,
                            "path": rel_path,
                            "size": fsize,
                            "format": fmt,
                            "url": f"/textures/{urllib.parse.quote(rel_path)}",
                            "preview": f"/textures/{urllib.parse.quote(preview_rel)}",
                        })

        items.sort(key=lambda x: x["name"])
        payload = json.dumps({
            "count": len(items),
            "textures": items,
            "textures_dir": self.textures_dir,
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_server(
    port: int = 8080,
    models_dir: str | None = None,
    textures_dir: str | None = None,
    open_browser: bool = True,
) -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    viewer_dir = os.path.join(root, "viewer")

    # Auto-locate candidate directories
    if not models_dir or not os.path.isdir(models_dir):
        candidates = [
            os.path.join(root, "models"),
            os.path.join(root, "..", "batch_converted_glb"),
            os.path.join(root, "..", "extracted_models"),
        ]
        best_cand = None
        max_count = 0
        for cand in candidates:
            if os.path.isdir(cand):
                c = sum(1 for _, _, files in os.walk(cand) for f in files if f.lower().endswith(('.glb', '.gltf')))
                if c > max_count:
                    max_count = c
                    best_cand = cand
        if best_cand:
            models_dir = best_cand
        if not models_dir or not os.path.isdir(models_dir):
            models_dir = os.path.join(root, "models")
            os.makedirs(models_dir, exist_ok=True)

    if not textures_dir or not os.path.isdir(textures_dir):
        candidates = [
            os.path.join(root, "textures"),
            os.path.join(root, "..", "clean_vehicles", "ttdb_vehicles"),
            os.path.join(root, "..", "viewable_pngs"),
            os.path.join(root, "..", "real_vehicle_pngs"),
        ]
        best_cand = None
        max_count = 0
        for cand in candidates:
            if os.path.isdir(cand):
                c = sum(1 for f in os.listdir(cand) if f.lower().endswith(('.png', '.dds', '.jpg', '.webp')))
                if c > max_count:
                    max_count = c
                    best_cand = cand
        if best_cand:
            textures_dir = best_cand
        if not textures_dir or not os.path.isdir(textures_dir):
            textures_dir = os.path.join(root, "textures")
            os.makedirs(textures_dir, exist_ok=True)

    StudioHandler.models_dir = os.path.abspath(models_dir)
    StudioHandler.textures_dir = os.path.abspath(textures_dir)
    StudioHandler.viewer_dir = viewer_dir

    addr = ("127.0.0.1", port)
    class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    try:
        httpd = ThreadingServer(addr, StudioHandler)
    except OSError:
        addr = ("127.0.0.1", port + 1)
        httpd = ThreadingServer(addr, StudioHandler)

    url = f"http://localhost:{addr[1]}"
    print(f"======================================================")
    print(f"       LEONIDA 3D ASSET STUDIO (VICE CITY EDITION)    ")
    print(f"======================================================")
    print(f"Web Studio URL:     {url}")
    print(f"Models Directory:   {StudioHandler.models_dir}")
    print(f"Textures Directory: {StudioHandler.textures_dir}")
    print(f"Viewer Directory:   {StudioHandler.viewer_dir}")
    print(f"Press Ctrl+C to stop.")

    if open_browser:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Leonida 3D Asset Studio local web gallery.")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default 8080)")
    parser.add_argument("--models-dir", type=str, default=None, help="Directory containing .glb models")
    parser.add_argument("--textures-dir", type=str, default=None, help="Directory containing textures")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")

    args = parser.parse_args(argv)
    start_server(
        port=args.port,
        models_dir=args.models_dir,
        textures_dir=args.textures_dir,
        open_browser=not args.no_browser,
    )
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
