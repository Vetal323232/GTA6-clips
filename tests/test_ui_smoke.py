"""Browser-driven smoke test of import -> analyze -> review -> export through the real local server.

Uses Playwright (dev dependency) with headless Chromium. Skipped when Playwright
or a browser is unavailable. Set GTA6CLIPS_CHROMIUM to a Chromium binary if the
Playwright-managed browser is not installed.
"""

import os
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from helpers import HAVE_FFMPEG, TempDir, make_app
from fixtures import make_bursts

from gta6clips.web import make_server

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


def launch(p):
    exe = os.environ.get("GTA6CLIPS_CHROMIUM")
    if exe:
        return p.chromium.launch(executable_path=exe)
    try:
        return p.chromium.launch()
    except Exception:
        fallback = "/opt/pw-browsers/chromium"
        if Path(fallback).exists():
            return p.chromium.launch(executable_path=fallback)
        raise


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = TempDir()
        cls.tmp = cls.td.__enter__()
        cls.folder = cls.tmp / "Мои записи GTA VI"
        cls.folder.mkdir()
        make_bursts(str(cls.folder / "заезд 1.mp4"), 40, [12, 30])
        cls.app = make_app(cls.tmp)
        cls.server = make_server(cls.app, port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.close()
        cls.td.__exit__(None, None, None)

    def test_foreign_host_rejected(self):
        req = urllib.request.Request(self.base + "/", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 403)

    def test_bad_import_shows_russian_error(self):
        data = "path=" + urllib.request.quote("C:/нет/такого/файла.mp4")
        req = urllib.request.Request(self.base + "/import", data=data.encode(), method="POST")
        with urllib.request.urlopen(req) as r:
            body = r.read().decode()
        self.assertIn("Путь не найден", body)

    @unittest.skipIf(sync_playwright is None, "playwright not installed")
    def test_browser_import_review_export(self):
        steps = []
        with sync_playwright() as p:
            browser = launch(p)
            page = browser.new_page()
            page.goto(self.base + "/")
            steps.append("открыта главная")
            page.fill("input[name=path]", str(self.folder))
            page.click("text=Импортировать")
            page.wait_for_selector("text=заезд 1.mp4")
            steps.append("импорт папки")
            page.click("text=заезд 1.mp4")
            page.uncheck("input[name=speech_enabled]")
            page.click("text=Запустить анализ")
            page.wait_for_selector("text=Найдено кандидатов", timeout=60000)
            jobs_before = self.app.db.one("SELECT COUNT(*) c FROM jobs")["c"]
            page.reload()
            page.wait_for_selector("text=Кандидаты")
            self.assertEqual(self.app.db.one("SELECT COUNT(*) c FROM jobs")["c"], jobs_before, "refresh duplicated job")
            steps.append("анализ с прогрессом, обновление страницы не создало задачу")
            page.click("table a[href^='/candidate/']")
            page.wait_for_selector("#player")
            steps.append("открыт кандидат с плеером")
            page.fill("input[name=start]", "10")
            page.fill("input[name=end]", "14.5")
            page.click("text=Сохранить границы")
            page.wait_for_selector("text=Границы сохранены")
            page.fill("input[name=reason]", "хороший взрыв")
            page.click("button[value=accepted]")
            page.wait_for_selector("text=Решение сохранено")
            steps.append("границы изменены, кандидат принят")
            page.select_option("select[name=layout]", "crop_blur")
            page.wait_for_function("document.getElementById('preview').src.includes('crop_blur')")
            page.wait_for_function("document.getElementById('preview').complete && document.getElementById('preview').naturalWidth > 0")
            page.click("text=Сохранить вид")
            page.wait_for_selector("text=Вид сохранён")
            steps.append("превью обрезки обновилось")
            page.click("text=Экспортировать этот клип")
            page.wait_for_selector("a[href^='/media/export/']", timeout=120000)
            steps.append("экспорт завершён")
            page.goto(self.base + "/exports")
            page.wait_for_selector("text=готово")
            page.goto(self.base + "/source/1/evaluation")
            page.wait_for_selector("text=Принято")
            steps.append("страница оценки")
            browser.close()
        ex = self.app.db.one("SELECT * FROM exports WHERE status='done' ORDER BY id DESC LIMIT 1")
        self.assertIsNotNone(ex)
        self.assertAlmostEqual(ex["duration"], 4.5, delta=0.2)
        self.assertEqual((ex["width"], ex["height"]), (1080, 1920))
        print("\nUI smoke steps: " + " → ".join(steps))


if __name__ == "__main__":
    unittest.main()
