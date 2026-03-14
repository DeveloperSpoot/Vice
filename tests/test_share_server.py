import asyncio
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vice import __version__
from vice.config import Config, OutputConfig, SharingConfig

try:
    from aiohttp import ClientSession
    from vice.share import ShareServer
except ModuleNotFoundError:
    ClientSession = None
    ShareServer = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _stub_ffprobe(_: Path) -> dict:
    return {"width": 1920, "height": 1080, "duration": 4.2}


@unittest.skipUnless(ShareServer is not None and ClientSession is not None, "aiohttp is not installed")
class ShareServerSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        root = Path(self.tmpdir.name)
        self.output_dir = root / "clips"
        self.output_dir.mkdir()
        self.thumb_dir = root / "thumbs"
        self.thumb_dir.mkdir()
        self.highlights_dir = root / "highlights"
        self.highlights_dir.mkdir()

        self.clip_path = self.output_dir / "test_clip.mp4"
        self.clip_path.write_bytes(b"not-a-real-mp4")

        self.thumb_path = self.thumb_dir / "test_clip.jpg"
        self.thumb_path.write_bytes(b"jpeg")

        self.local_port = _free_port()
        self.public_port = _free_port()
        while self.public_port == self.local_port:
            self.public_port = _free_port()

        async def _stub_make_thumb(_: Path) -> Path:
            return self.thumb_path

        self.triggered = asyncio.Event()

        self.patchers = [
            mock.patch("vice.share._local_ip", return_value="127.0.0.1"),
            mock.patch("vice.share.THUMB_DIR", self.thumb_dir),
            mock.patch("vice.share.HIGHLIGHTS_DIR", self.highlights_dir),
            mock.patch("vice.share._ffprobe", new=_stub_ffprobe),
            mock.patch("vice.share._make_thumb", new=_stub_make_thumb),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        cfg = Config(
            output=OutputConfig(directory=str(self.output_dir)),
            sharing=SharingConfig(
                port=self.local_port,
                public_port=self.public_port,
                cloudflare_tunnel=False,
            ),
        )
        self.server = ShareServer(cfg)

        async def _trigger() -> None:
            self.triggered.set()

        self.server.trigger_clip_cb = _trigger
        self.server.get_status_cb = lambda: {"recording": True, "backend": "test"}

        await self.server.start()
        self.server.add_clip(self.clip_path)
        self.client = ClientSession()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.stop()

    async def test_local_control_server_exposes_ui_api_and_ws(self) -> None:
        local_base = self.server.local_base_url()
        self.assertEqual(local_base, f"http://127.0.0.1:{self.local_port}")

        async with self.client.get(f"{local_base}/api/clips") as resp:
            self.assertEqual(resp.status, 200)
            payload = await resp.json()
        self.assertEqual(payload["clips"][0]["slug"], "test_clip")
        self.assertEqual(
            payload["clips"][0]["share_url"],
            f"http://127.0.0.1:{self.public_port}/c/test_clip",
        )

        async with self.client.get(f"{local_base}/api/status") as resp:
            self.assertEqual(resp.status, 200)
            status = await resp.json()
        self.assertEqual(status["local_url"], local_base)
        self.assertEqual(status["public_url"], f"http://127.0.0.1:{self.public_port}")

        async with self.client.post(f"{local_base}/api/trigger") as resp:
            self.assertEqual(resp.status, 200)
        await asyncio.wait_for(self.triggered.wait(), timeout=1.0)

        ws = await self.client.ws_connect(f"ws://127.0.0.1:{self.local_port}/ws")
        await ws.close()

    async def test_public_server_only_serves_share_routes(self) -> None:
        public_base = f"http://127.0.0.1:{self.public_port}"

        async with self.client.get(f"{public_base}/c/test_clip") as resp:
            self.assertEqual(resp.status, 200)
            html = await resp.text()
        self.assertIn(f"{public_base}/v/test_clip", html)

        async with self.client.get(f"{public_base}/v/test_clip") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "video/mp4")

        async with self.client.get(f"{public_base}/t/test_clip") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/jpeg")

    async def test_public_server_blocks_privileged_routes_and_mutation(self) -> None:
        public_base = f"http://127.0.0.1:{self.public_port}"

        async with self.client.get(f"{public_base}/") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.get(f"{public_base}/api/clips") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.post(f"{public_base}/api/trigger") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.get(f"{public_base}/ws") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.delete(f"{public_base}/api/clips/test_clip") as resp:
            self.assertEqual(resp.status, 404)

        self.assertTrue(self.clip_path.exists())


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerBaseUrlTests(unittest.TestCase):
    def test_configured_public_base_url_beats_tunnel_and_bind_url(self) -> None:
        cfg = Config(
            sharing=SharingConfig(
                base_url="https://clips.example.com/",
                port=8765,
                public_port=8766,
                cloudflare_tunnel=False,
            )
        )
        server = ShareServer(cfg)
        server._tunnel_url = "https://ignored.trycloudflare.com"
        server._public_bind_url = "http://127.0.0.1:8766"

        self.assertEqual(server.public_base_url(), "https://clips.example.com")


@unittest.skipUnless(ShareServer is not None, "aiohttp is not installed")
class ShareServerUiVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ui_response_injects_current_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ui_path = Path(tmp) / "index.html"
            ui_path.write_text("<div>Version __VICE_VERSION__</div>", encoding="utf-8")
            server = ShareServer(Config())

            with mock.patch("vice.share._resolve_ui_index", return_value=ui_path):
                response = await server._ui(mock.Mock())

        self.assertEqual(response.status, 200)
        self.assertIn(__version__, response.text)
        self.assertNotIn("__VICE_VERSION__", response.text)


@unittest.skipUnless(ShareServer is not None and ClientSession is not None, "aiohttp is not installed")
class ShareServerLegacyUrlCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        root = Path(self.tmpdir.name)
        self.output_dir = root / "clips"
        self.output_dir.mkdir()
        self.thumb_dir = root / "thumbs"
        self.thumb_dir.mkdir()
        self.highlights_dir = root / "highlights"
        self.highlights_dir.mkdir()

        self.clip_path = self.output_dir / "legacy_clip.mp4"
        self.clip_path.write_bytes(b"not-a-real-mp4")

        self.thumb_path = self.thumb_dir / "legacy_clip.jpg"
        self.thumb_path.write_bytes(b"jpeg")

        self.local_port = _free_port()
        self.public_port = _free_port()
        while self.public_port == self.local_port:
            self.public_port = _free_port()

        async def _stub_make_thumb(_: Path) -> Path:
            return self.thumb_path

        self.patchers = [
            mock.patch("vice.share._local_ip", return_value="127.0.0.2"),
            mock.patch("vice.share.THUMB_DIR", self.thumb_dir),
            mock.patch("vice.share.HIGHLIGHTS_DIR", self.highlights_dir),
            mock.patch("vice.share._ffprobe", new=_stub_ffprobe),
            mock.patch("vice.share._make_thumb", new=_stub_make_thumb),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        cfg = Config(
            output=OutputConfig(directory=str(self.output_dir)),
            sharing=SharingConfig(
                port=self.local_port,
                public_port=self.public_port,
                cloudflare_tunnel=False,
            ),
        )
        self.server = ShareServer(cfg)

        await self.server.start()
        self.server.add_clip(self.clip_path)
        self.client = ClientSession()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.stop()

    async def test_legacy_pre_v1_0_12_share_urls_still_resolve(self) -> None:
        legacy_base = f"http://127.0.0.2:{self.local_port}"

        async with self.client.get(f"{legacy_base}/c/legacy_clip") as resp:
            self.assertEqual(resp.status, 200)
            html = await resp.text()
        self.assertIn(f"{legacy_base}/v/legacy_clip", html)

        async with self.client.get(f"{legacy_base}/v/legacy_clip") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "video/mp4")

        async with self.client.get(f"{legacy_base}/t/legacy_clip") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/jpeg")

    async def test_legacy_origin_still_blocks_ui_and_api_routes(self) -> None:
        legacy_base = f"http://127.0.0.2:{self.local_port}"

        async with self.client.get(f"{legacy_base}/") as resp:
            self.assertEqual(resp.status, 404)

        async with self.client.get(f"{legacy_base}/api/clips") as resp:
            self.assertEqual(resp.status, 404)
