"""Local review UI: stdlib HTTP server bound to 127.0.0.1, server-rendered HTML.

No Node.js or frontend build step. All state lives in SQLite; every POST
redirects (Post/Redirect/Get), so refreshing a page never re-submits a job.
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from .app import App
from .db import loads
from .media import MediaError, Runner
from .render import LAYOUTS, normalize_crop, preview_frame
from .scoring import SIGNAL_LABELS, fmt_t
from .speech import ESTIMATED_MODEL_SIZE, runtime_available
from .subtitles import to_srt

STATUS_RU = {"pending": "не просмотрен", "accepted": "принят", "rejected": "отклонён"}
JOB_RU = {"queued": "в очереди", "running": "выполняется", "done": "готово", "failed": "ошибка",
          "cancelled": "отменено", "interrupted": "прервано"}

CSS = """
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#f5f5f7;color:#1d1d1f}
header{background:#1d1d1f;color:#fff;padding:10px 20px;display:flex;gap:20px;align-items:center}
header a{color:#fff;text-decoration:none}main{max-width:1100px;margin:0 auto;padding:16px 20px}
section{background:#fff;border-radius:10px;padding:14px 18px;margin:14px 0;box-shadow:0 1px 2px #0001}
h1{font-size:22px}h2{font-size:17px;margin-top:0}table{border-collapse:collapse;width:100%}
td,th{padding:6px 8px;border-bottom:1px solid #eee;text-align:left;vertical-align:top;font-size:14px}
.msg{background:#e7f6ec;border:1px solid #9bd3ad;padding:8px 12px;border-radius:8px;white-space:pre-wrap}
.err{background:#fdecec;border:1px solid #f1a5a5;padding:8px 12px;border-radius:8px;white-space:pre-wrap}
.muted{color:#6e6e73;font-size:13px}.bar{background:#eee;border-radius:6px;height:12px;overflow:hidden}
.bar>div{background:#0a84ff;height:100%;width:0}button,.btn{padding:6px 12px;border-radius:6px;border:1px solid #bbb;
background:#fafafa;cursor:pointer;font-size:14px;text-decoration:none;color:#1d1d1f;display:inline-block}
button.primary{background:#0a84ff;color:#fff;border-color:#0a84ff}input[type=text],input[type=number],select{padding:5px;font-size:14px}
input[type=text].wide{width:100%;box-sizing:border-box}label{display:inline-block;margin:4px 12px 4px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.tag{font-size:12px;padding:2px 6px;border-radius:4px;background:#eee}
.accepted{background:#d8f5df}.rejected{background:#f9dcdc}video{max-width:100%;background:#000}
img.preview{border:1px solid #ccc;max-height:640px}pre{white-space:pre-wrap;font-size:12px}
"""

JS = """
function pollJob(el){const id=el.dataset.job;const bar=el.querySelector('.bar>div');const txt=el.querySelector('.jobtext');
 const tick=()=>fetch('/api/job/'+id).then(r=>r.json()).then(j=>{bar.style.width=Math.round((j.progress||0)*100)+'%';
 txt.textContent=j.status_ru+' — '+(j.message||'');if(j.active){setTimeout(tick,1000)}else if(!el.dataset.loaded){
 el.dataset.loaded=1;location.reload()}}).catch(()=>setTimeout(tick,3000));tick();}
document.querySelectorAll('[data-job]').forEach(el=>{if(el.dataset.active==='1')pollJob(el)});
const v=document.getElementById('player');
if(v){const s=parseFloat(v.dataset.start),e=parseFloat(v.dataset.end);
 v.addEventListener('loadedmetadata',()=>{v.currentTime=s});
 v.addEventListener('timeupdate',()=>{if(v.dataset.loop==='1'&&v.currentTime>e){v.pause();v.currentTime=s}});
 document.querySelectorAll('[data-settime]').forEach(b=>b.addEventListener('click',ev=>{ev.preventDefault();
  document.querySelector('input[name='+b.dataset.settime+']').value=v.currentTime.toFixed(2)}));
 document.querySelectorAll('[data-jump]').forEach(b=>b.addEventListener('click',ev=>{ev.preventDefault();
  v.currentTime=parseFloat(b.dataset.jump);v.play()}));}
const pf=document.getElementById('layoutform');
if(pf){const img=document.getElementById('preview');const upd=()=>{const q=new URLSearchParams(new FormData(pf));
 q.delete('burn');img.src=img.dataset.base+'?'+q.toString();
 pf.querySelectorAll('input[type=range]').forEach(r=>{const o=document.getElementById(r.name+'_val');if(o)o.textContent=r.value})};
 let t;pf.addEventListener('input',()=>{clearTimeout(t);t=setTimeout(upd,300)});}
"""


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def page(title: str, body: str, msg: str = "", err: str = "") -> str:
    notes = (f'<div class="msg">{esc(msg)}</div>' if msg else "") + (f'<div class="err">{esc(err)}</div>' if err else "")
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{esc(title)} — GTA6 clips</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head><body>'
            f'<header><b>GTA6 clips</b><a href="/">Источники</a><a href="/exports">Экспорт</a></header>'
            f'<main>{notes}{body}</main><script>{JS}</script></body></html>')


def job_widget(row) -> str:
    if row is None:
        return ""
    active = row["status"] in ("queued", "running")
    pct = int((row["progress"] or 0) * 100)
    cancel = (f'<form method="post" action="/job/{row["id"]}/cancel" style="display:inline">'
              f'<button>Отменить</button></form>') if active else ""
    err = f'<div class="err">{esc(row["error"])}</div>' if row["error"] else ""
    kind = "Анализ" if row["kind"] == "analyze" else "Экспорт"
    return (f'<div data-job="{row["id"]}" data-active="{int(active)}" id="job"><b>{kind} (задача №{row["id"]})</b> '
            f'<span class="jobtext">{esc(JOB_RU.get(row["status"], row["status"]))} — {esc(row["message"])}</span> {cancel}'
            f'<div class="bar"><div style="width:{pct}%"></div></div>{err}</div>')


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "gta6clips"

    # -------------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):  # quieter console
        if os.environ.get("GTA6CLIPS_HTTP_LOG"):
            super().log_message(fmt, *args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if self.command == "POST" and origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return False
        return True

    def send_html(self, body: str, status=HTTPStatus.OK):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=HTTPStatus.OK):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data: bytes, ctype: str, filename: str | None = None):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, url: str, msg: str = "", err: str = ""):
        q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
        if q:
            url += ("&" if "?" in url else "?") + urlencode(q)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return parse_qs(raw, keep_blank_values=True)

    def not_found(self, what="Страница не найдена"):
        self.send_html(page("Не найдено", f"<h1>{esc(what)}</h1>"), HTTPStatus.NOT_FOUND)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        if not self._host_ok():
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden host")
            return
        url = urlparse(self.path)
        self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        for pattern, name in ROUTES[method]:
            m = re.fullmatch(pattern, url.path)
            if m:
                try:
                    getattr(self, name)(*[int(g) if g.isdigit() else g for g in m.groups()])
                except (MediaError, ValueError) as e:
                    back = self.headers.get("Referer") or "/"
                    back = urlparse(back).path or "/"
                    if method == "POST":
                        self.redirect(back, err=str(e))
                    else:
                        self.send_html(page("Ошибка", "", err=str(e)), HTTPStatus.BAD_REQUEST)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
        self.not_found()

    # ------------------------------------------------------------------ pages
    def home(self):
        a = self.app
        sources = a.db.all("SELECT s.*, (SELECT COUNT(*) FROM candidates c JOIN analyses an ON an.id=c.analysis_id "
                           "WHERE c.source_id=s.id AND an.fingerprint=s.fingerprint AND an.id=(SELECT MAX(id) FROM analyses "
                           "WHERE source_id=s.id AND fingerprint=s.fingerprint)) AS n_cand FROM sources s ORDER BY s.id DESC")
        rows = "".join(
            f'<tr><td><a href="/source/{s["id"]}">{esc(s["filename"])}</a><div class="muted">{esc(s["path"])}</div></td>'
            f'<td>{fmt_t(s["duration"] or 0)}</td><td>{"есть" if s["has_audio"] else "нет"}</td>'
            f'<td>{s["n_cand"] or 0}</td><td>{"ок" if s["status"] == "ok" else esc(s["error"] or s["status"])}</td></tr>'
            for s in sources)
        table = (f'<table><tr><th>Файл</th><th>Длительность</th><th>Звук</th><th>Кандидаты</th><th>Состояние</th></tr>{rows}</table>'
                 if sources else '<p class="muted">Пока нет ни одного источника. Импортируйте файл или папку.</p>')
        jobs = a.db.all("SELECT * FROM jobs ORDER BY id DESC LIMIT 8")
        jrows = "".join(f'<tr><td>№{j["id"]}</td><td>{"анализ" if j["kind"] == "analyze" else "экспорт"}</td>'
                        f'<td>{esc(JOB_RU.get(j["status"]))}</td><td>{esc(j["message"])}</td>'
                        f'<td>{"" if j["elapsed"] is None else f"{j["elapsed"]:.1f} с"}</td></tr>' for j in jobs)
        ok, reason = runtime_available()
        sp = a.cfg["speech"]
        speech = (f'включено, модель «{esc(sp["model"])}», устройство {esc(sp["device"])}/{esc(sp["compute_type"])}; '
                  f'первая загрузка {ESTIMATED_MODEL_SIZE.get(sp["model"], "")}' if sp["enabled"] and ok
                  else ("отключено в настройках" if not sp["enabled"] else "недоступно: " + esc(reason)))
        body = f"""
<h1>Источники</h1>
<section><h2>Импорт</h2>
<form method="post" action="/import">
<p>Вставьте полный путь к видеофайлу или к папке с записями (например <code>D:\\Записи\\GTA VI</code>).
Браузер не передаёт полный путь из окна выбора файла, поэтому путь вводится текстом. В проводнике Windows:
Shift + правый клик по файлу → «Копировать как путь».</p>
<input type="text" class="wide" name="path" placeholder="C:\\Users\\...\\Videos\\запись.mp4 или папка" required>
<label><input type="checkbox" name="recursive" checked> включая вложенные папки</label><br>
<label>Автор / источник: <input type="text" name="attribution" size="30"></label>
<label>Права на использование: <input type="text" name="usage_notes" size="40"></label><br>
<button class="primary">Импортировать</button></form></section>
<section><h2>Записи</h2>{table}</section>
<section><h2>Последние задачи</h2>{f"<table>{jrows}</table>" if jobs else '<p class="muted">Задач ещё не было.</p>'}</section>
<section><h2>Настройки</h2><table>
<tr><td>Игра</td><td>{esc(a.cfg["general"]["game"])}</td></tr>
<tr><td>Файл настроек</td><td>{esc(a.cfg.path or "не найден, используются значения по умолчанию")}</td></tr>
<tr><td>Папка данных</td><td>{esc(a.data_dir)}</td></tr>
<tr><td>Папка экспорта</td><td>{esc(a.cfg.output_dir)}</td></tr>
<tr><td>Распознавание речи</td><td>{speech}</td></tr>
<tr><td>Twitch</td><td>Этап 2 — пока не реализовано</td></tr></table></section>"""
        self.send_html(page("Источники", body, self.query.get("msg", ""), self.query.get("err", "")))

    def do_import(self):
        f = self.form()
        path = (f.get("path") or [""])[0].strip()
        if not path:
            raise ValueError("Укажите путь к файлу или папке")
        res = self.app.import_path(path, bool(f.get("recursive")), (f.get("attribution") or [""])[0],
                                   (f.get("usage_notes") or [""])[0])
        status_ru = {"new": "добавлен", "existing": "уже был (дубликат не создан)", "modified": "файл изменился, обновлён"}
        ok = [f'{r["name"]}: {status_ru[r["status"]]}' for r in res if r["status"] != "error"]
        bad = [f'{r["name"]}: {r["error"]}' for r in res if r["status"] == "error"]
        if len(res) == 1 and ok:
            self.redirect(f'/source/{res[0]["id"]}', msg=ok[0])
        else:
            self.redirect("/", msg="\n".join(ok), err="\n".join(bad))

    def source_page(self, sid: int):
        a = self.app
        s = a.source(sid)
        if s is None:
            return self.not_found("Источник не найден")
        an = a.latest_analysis(sid)
        job = a.db.one("SELECT * FROM jobs WHERE source_id=? ORDER BY (status IN ('queued','running')) DESC, id DESC LIMIT 1", (sid,))
        an_cfg = a.cfg["analysis"]
        sp = a.cfg["speech"]
        params = loads(an["params"]) if an else None
        cur = params["analysis"] if params else an_cfg
        cur_sp = params["speech"] if params else sp

        def num(name, label, step="0.5"):
            return f'<label>{label} <input type="number" step="{step}" name="{name}" value="{esc(cur[name])}" style="width:70px"></label>'

        def chk(name, label, val):
            return f'<label><input type="checkbox" name="{name}" {"checked" if val else ""}> {label}</label>'

        form = f"""<form method="post" action="/source/{sid}/analyze">
{num("sensitivity", "Чувствительность (ст. откл.)", "0.1")}{num("before", "Секунд до события")}{num("after", "Секунд после")}
{num("target_min", "Желаемая мин. длина")}{num("max_len", "Макс. длина")}{num("candidates_per_source", "Кандидатов", "1")}<br>
{chk("visual", "Анализ изображения (смена сцен)", cur["visual"])}{chk("vad", "Определение речи (VAD)", cur["vad"])}
{chk("speech_enabled", "Распознавание речи (Whisper, локально)", cur_sp["enabled"])}
<label>Язык речи <input type="text" name="speech_language" value="{esc(cur_sp.get("language") or "")}" size="4" placeholder="авто"></label>
<br><button class="primary">Запустить анализ</button></form>"""
        signals = ""
        cands_html = ""
        if an:
            st = loads(an["signals"], {})
            names = {"audio": "Звук", "vad": "VAD (речь/тишина)", "visual": "Изображение", "speech": "Распознавание речи",
                     "cues": "Ключевые фразы", "hud": "HUD-детектор", "cache": "Кэш"}
            signals = "".join(f'<tr><td>{names.get(k, k)}</td><td>{"доступно" if v.get("available") else "недоступно"}</td>'
                              f'<td class="muted">{esc(v.get("reason"))} {esc(json.dumps(v.get("info"), ensure_ascii=False) if v.get("info") else "")}</td></tr>'
                              for k, v in st.items())
            signals = f"<h2>Сигналы последнего анализа</h2><table>{signals}</table>"
            cands = a.candidates(an["id"])
            if cands:
                rows = "".join(
                    f'<tr class="{c["status"]}"><td><input type="checkbox" name="ids" value="{c["id"]}" '
                    f'{"checked" if c["status"] == "accepted" else ""}></td><td>{c["rank"]}</td>'
                    f'<td><a href="/candidate/{c["id"]}">{fmt_t(c["start"])}–{fmt_t(c["end"])}</a>'
                    f'{" ✎" if c["edited"] else ""}</td><td>{c["end"] - c["start"]:.1f} с</td><td>{c["score"]:.2f}</td>'
                    f'<td>{STATUS_RU[c["status"]]}</td><td class="muted">{esc(c["explanation"][:180])}…</td></tr>'
                    for c in cands)
                limit = a.cfg["export"]["max_clips_per_run"]
                cands_html = f"""<form method="post" action="/source/{sid}/export"><table>
<tr><th></th><th>№</th><th>Фрагмент</th><th>Длина</th><th>Оценка</th><th>Статус</th><th>Почему выбран</th></tr>{rows}</table>
<p><label><input type="checkbox" name="force"> экспортировать заново (новый файл, старый не перезаписывается)</label>
<button class="primary">Экспортировать отмеченные (не больше {limit})</button></p></form>"""
            else:
                cands_html = f'<p class="muted">Кандидатов нет. {esc(a.db.one("SELECT message FROM jobs WHERE id=?", (an["job_id"],))["message"] if an["job_id"] else "")}</p>'
        else:
            cands_html = '<p class="muted">Анализ ещё не запускался.</p>'
        marks = a.db.all("SELECT * FROM marked_intervals WHERE source_id=? ORDER BY start", (sid,))
        mrows = "".join(f'<li>{fmt_t(m["start"])}–{fmt_t(m["end"])} {esc(m["note"] or "")}</li>' for m in marks)
        body = f"""<h1>{esc(s["filename"])}</h1>
<section><table><tr><td>Путь</td><td>{esc(s["path"])}</td></tr>
<tr><td>Длительность</td><td>{fmt_t(s["duration"] or 0)}; {s["width"]}x{s["height"]}; {s["fps"] and round(s["fps"], 2)} к/с; звук: {"есть" if s["has_audio"] else "нет"}</td></tr>
<tr><td>Состояние</td><td>{"ок" if s["status"] == "ok" else esc(s["error"])}</td></tr></table>
<form method="post" action="/source/{sid}/meta"><label>Автор / источник: <input type="text" name="attribution" value="{esc(s["attribution"] or "")}" size="30"></label>
<label>Права: <input type="text" name="usage_notes" value="{esc(s["usage_notes"] or "")}" size="40"></label><button>Сохранить</button></form></section>
<section><h2>Анализ</h2>{form}{job_widget(job)}</section>
<section>{signals}<h2>Кандидаты</h2>{cands_html}</section>
<section><h2>Отметить интересный момент вручную</h2><p class="muted">Нужно для оценки качества: показывает, какие отмеченные
моменты программа пропустила. Время — секунды или мм:сс.</p><form method="post" action="/source/{sid}/mark">
<label>Начало <input type="text" name="start" size="8"></label><label>Конец <input type="text" name="end" size="8"></label>
<label>Заметка <input type="text" name="note" size="30"></label><button>Отметить</button></form><ul>{mrows}</ul>
<a class="btn" href="/source/{sid}/evaluation">Оценка качества</a></section>"""
        self.send_html(page(s["filename"], body, self.query.get("msg", ""), self.query.get("err", "")))

    def source_meta(self, sid: int):
        f = self.form()
        self.app.db.update("sources", sid, {"attribution": (f.get("attribution") or [""])[0] or None,
                                            "usage_notes": (f.get("usage_notes") or [""])[0] or None})
        self.redirect(f"/source/{sid}", msg="Сохранено")

    def source_analyze(self, sid: int):
        f = self.form()
        ov = {}
        for k in ("sensitivity", "before", "after", "target_min", "max_len"):
            if f.get(k) and f[k][0].strip():
                ov[k] = float(f[k][0].replace(",", "."))
        if f.get("candidates_per_source") and f["candidates_per_source"][0].strip():
            ov["candidates_per_source"] = int(f["candidates_per_source"][0])
        if "max_len" in ov and ov["max_len"] <= 0:
            raise ValueError("Максимальная длина должна быть больше нуля")
        ov["visual"] = bool(f.get("visual"))
        ov["vad"] = bool(f.get("vad"))
        ov["speech_enabled"] = bool(f.get("speech_enabled"))
        ov["speech_language"] = (f.get("speech_language") or [""])[0].strip()
        jid, created = self.app.start_analysis(sid, ov)
        self.redirect(f"/source/{sid}", msg="Анализ запущен" if created else "Такой анализ уже выполняется — новая задача не создана")

    def source_export(self, sid: int):
        f = self.form()
        ids = [int(x) for x in f.get("ids", [])]
        jid, created = self.app.start_export(ids, bool(f.get("force")))
        self.redirect(f"/source/{sid}", msg="Экспорт запущен" if created else "Этот экспорт уже выполняется")

    def source_mark(self, sid: int):
        f = self.form()
        s, e = parse_time((f.get("start") or [""])[0]), parse_time((f.get("end") or [""])[0])
        self.app.mark_interval(sid, s, e, (f.get("note") or [""])[0])
        self.redirect(f"/source/{sid}", msg="Интервал отмечен")

    def source_evaluation(self, sid: int):
        r = self.app.evaluation(sid, save=self.query.get("save") == "1")
        s = self.app.source(sid)
        topk = "".join(f'<tr><td>Топ-{t["k"]}</td><td>{t["candidates"]}</td><td>{t["reviewed"]}</td><td>{t["accepted"]}</td></tr>'
                       for t in r["top_k"])
        missed = "".join(f'<li>{fmt_t(m["start"])}–{fmt_t(m["end"])} {esc(m["note"] or "")}</li>' for m in r["marks_missed"])
        notes = "".join(f'<li>№{n["rank"]} ({STATUS_RU[n["status"]]}): {esc(n["reason"] or "")}'
                        f'{" — границы изменены вручную: авто " + fmt_t(n["auto"][0]) + "–" + fmt_t(n["auto"][1]) + ", итог " + fmt_t(n["final"][0]) + "–" + fmt_t(n["final"][1]) if n["edited"] else ""}</li>'
                        for n in r["boundary_notes"])
        jobs = "".join(f'<tr><td>№{j["job_id"]}</td><td>{j["elapsed_s"]} с</td><td>{j["peak_rss_mb"] or "—"} МБ</td>'
                       f'<td>{esc(", ".join(j["signals_enabled"]))}</td><td class="muted">{esc(json.dumps(j["hardware"], ensure_ascii=False))}</td></tr>'
                       for j in r["jobs"])
        body = f"""<h1>Оценка качества: {esc(s["filename"])}</h1>
<section><p class="muted">Это оценка полезности найденных моментов по вашим решениям. Техническая правильность проверяется
тестами отдельно. Решения «отклонить» не используются для автоматического обучения.</p>
<table><tr><td>Кандидатов</td><td>{r["total"]}</td></tr><tr><td>Просмотрено</td><td>{r["reviewed"]}</td></tr>
<tr><td>Принято</td><td>{r["accepted"]}</td></tr><tr><td>Отклонено</td><td>{r["rejected"]}</td></tr>
<tr><td>Ещё не просмотрено</td><td>{r["pending"]}</td></tr></table></section>
<section><h2>Принятые в топ-K</h2><table><tr><th></th><th>Кандидатов</th><th>Просмотрено</th><th>Принято</th></tr>{topk}</table></section>
<section><h2>Пропущенные отмеченные моменты</h2><p class="muted">Правило: {esc(r["rule"])}. Отмечено: {r["marks_total"]}.</p>
<ul>{missed or "<li>нет</li>"}</ul></section>
<section><h2>Заметки о границах и решениях</h2><ul>{notes or "<li>нет</li>"}</ul></section>
<section><h2>Задачи анализа</h2><table><tr><th>Задача</th><th>Время</th><th>Пик памяти</th><th>Сигналы</th><th>Оборудование</th></tr>{jobs}</table>
<a class="btn" href="/source/{sid}/evaluation?save=1">Сохранить отчёт в базу</a></section>"""
        self.send_html(page("Оценка", body, "Отчёт сохранён" if self.query.get("save") == "1" else ""))

    def candidate_page(self, cid: int):
        a = self.app
        c = a.candidate(cid)
        if c is None:
            return self.not_found("Кандидат не найден")
        s = a.source(c["source_id"])
        settings = a.export_settings(c)
        crop = settings["crop"]
        contrib = loads(c["contributions"], {})
        crows = "".join(
            f'<tr><td>{SIGNAL_LABELS[k]}</td>' + (
                f'<td>{v["value"]:.2f}</td><td>{v["weighted"]:.2f}</td><td>{v["max_in_clip"]:.2f}</td>' if v.get("available")
                else f'<td colspan="3" class="muted">недоступно: {esc(v.get("reason"))}</td>') + "</tr>"
            for k, v in contrib.items() if not k.startswith("_"))
        flags = loads(c["flags"], [])
        flag_html = f'<div class="err">Внимание: {esc("; ".join(flags))}</div>' if flags else ""
        cues = a.clip_subtitles(cid)
        subs = "".join(f"<li>{s_.start:.2f}–{s_.end:.2f}: {esc(s_.text)}</li>" for s_ in cues[:50])
        layout_opts = "".join(f'<option value="{k}" {"selected" if k == settings["layout"] else ""}>{esc(v)}</option>'
                              for k, v in LAYOUTS.items())

        def slider(name, label):
            return (f'<label>{label} <input type="range" min="0" max="1" step="0.01" name="{name}" value="{crop[name]}">'
                    f' <span id="{name}_val">{crop[name]}</span></label>')

        exports = a.db.all("SELECT * FROM exports WHERE candidate_id=? ORDER BY id DESC", (cid,))
        erows = "".join(f'<li>{esc(Path(e["path"]).name)} — {esc({"done": "готово", "failed": "ошибка", "running": "идёт", "cancelled": "отменён"}.get(e["status"], e["status"]))}'
                        f'{" (повторный)" if e["forced"] else ""}'
                        f'{f" — <a href=/media/export/{e["id"]}>открыть</a>" if e["status"] == "done" else ""}'
                        f'{f" <span class=muted>{esc(e["error"][:300])}</span>" if e["error"] else ""}</li>' for e in exports)
        job = a.db.one("SELECT * FROM jobs WHERE kind='export' AND source_id=? ORDER BY id DESC LIMIT 1", (c["source_id"],))
        body = f"""<p><a href="/source/{s["id"]}">← {esc(s["filename"])}</a></p>
<h1>Кандидат №{c["rank"]}: {fmt_t(c["start"])}–{fmt_t(c["end"])} ({c["end"] - c["start"]:.1f} с)</h1>{flag_html}
<div class="grid"><section><video id="player" controls preload="metadata" data-loop="1" data-start="{c["start"]}" data-end="{c["end"]}"
src="/media/source/{s["id"]}#t={c["start"]}"></video>
<p><button data-jump="{c["start"]}">▶ С начала клипа</button> <button data-jump="{max(0, c["anchor"] - 2)}">▶ Ключевой момент ({fmt_t(c["anchor"])})</button></p>
<form method="post" action="/candidate/{cid}/edit"><label>Начало, с <input type="text" name="start" value="{c["start"]}" size="8"></label>
<button data-settime="start">← текущее время</button><br><label>Конец, с <input type="text" name="end" value="{c["end"]}" size="8"></label>
<button data-settime="end">← текущее время</button><br><button class="primary">Сохранить границы</button></form>
<form method="post" action="/candidate/{cid}/reset"><button>Вернуть автоматические ({fmt_t(c["auto_start"])}–{fmt_t(c["auto_end"])})</button></form>
<h2>Решение</h2><p>Сейчас: <b>{STATUS_RU[c["status"]]}</b> {esc(c["reason"] or "")}</p>
<form method="post" action="/candidate/{cid}/review"><input type="text" name="reason" placeholder="причина (необязательно)" size="30">
<button name="status" value="accepted">Принять</button> <button name="status" value="rejected">Отклонить</button>
<button name="status" value="pending">Сбросить</button></form></section>
<section><h2>Кадр для вертикального видео</h2>
<form method="post" action="/candidate/{cid}/layout" id="layoutform"><select name="layout">{layout_opts}</select><br>
{slider("x", "X")}{slider("y", "Y")}<br>{slider("w", "Ширина")}{slider("h", "Высота")}<br>
<input type="hidden" name="t" value="{c["anchor"]}">
<label><input type="checkbox" name="burn" {"checked" if settings["burn_subtitles"] else ""}> вшить субтитры</label>
<button>Сохранить вид</button></form><p class="muted">Область обрезки используется только в режимах «Выбранная область».</p>
<img class="preview" id="preview" data-base="/candidate/{cid}/preview.jpg" src="/candidate/{cid}/preview.jpg?layout={settings["layout"]}&x={crop["x"]}&y={crop["y"]}&w={crop["w"]}&h={crop["h"]}&t={c["anchor"]}" alt="превью"></section></div>
<section><h2>Почему выбран</h2><p>{esc(c["explanation"])}</p><table><tr><th>Сигнал</th><th>В ключевой момент</th><th>Вклад в оценку</th><th>Максимум в клипе</th></tr>{crows}</table>
<p class="muted">Источник: {esc(s["path"])}; автор: {esc(s["attribution"] or "не указан")}; права: {esc(s["usage_notes"] or "не указаны")}</p></section>
<section><h2>Субтитры</h2>{f"<ul>{subs}</ul><a class=btn href=/candidate/{cid}/clip.srt>Скачать SRT</a>" if cues else '<p class="muted">Расшифровки для этого фрагмента нет (распознавание отключено, недоступно или речи нет).</p>'}</section>
<section><h2>Экспорт</h2>{job_widget(job) if job else ""}<form method="post" action="/candidate/{cid}/export">
<label><input type="checkbox" name="force"> заново (новый файл)</label><button class="primary">Экспортировать этот клип</button></form>
<ul>{erows}</ul><form method="post" action="/open-output"><button>Открыть папку экспорта</button></form></section>"""
        self.send_html(page("Кандидат", body, self.query.get("msg", ""), self.query.get("err", "")))

    def candidate_edit(self, cid: int):
        f = self.form()
        flags = self.app.edit_candidate(cid, parse_time(f["start"][0]), parse_time(f["end"][0]))
        self.redirect(f"/candidate/{cid}", msg="Границы сохранены", err=("; ".join(flags) if flags else ""))

    def candidate_reset(self, cid: int):
        self.app.reset_candidate(cid)
        self.redirect(f"/candidate/{cid}", msg="Возвращены автоматические границы")

    def candidate_review(self, cid: int):
        f = self.form()
        self.app.review(cid, f.get("status", ["pending"])[0], (f.get("reason") or [""])[0])
        self.redirect(f"/candidate/{cid}", msg="Решение сохранено")

    def candidate_layout(self, cid: int):
        f = self.form()
        crop = {k: (f.get(k) or [None])[0] for k in ("x", "y", "w", "h")}
        self.app.set_layout(cid, f.get("layout", ["fit_blur"])[0], crop, bool(f.get("burn")))
        self.redirect(f"/candidate/{cid}", msg="Вид сохранён")

    def candidate_export(self, cid: int):
        f = self.form()
        jid, created = self.app.start_export([cid], bool(f.get("force")))
        self.redirect(f"/candidate/{cid}", msg="Экспорт запущен" if created else "Этот экспорт уже выполняется")

    def candidate_preview(self, cid: int):
        c = self.app.candidate(cid)
        s = self.app.source(c["source_id"])
        q = self.query
        layout = q.get("layout", "fit_blur")
        if layout not in LAYOUTS:
            raise ValueError("Неизвестный вид")
        t = float(q.get("t", c["anchor"]))
        data = preview_frame(s["path"], min(t, float(s["duration"]) - 0.1), layout,
                             normalize_crop({k: q.get(k) for k in "xywh"}), self.app.cfg["export"], Runner())
        self.send_bytes(data, "image/jpeg")

    def candidate_srt(self, cid: int):
        cues = self.app.clip_subtitles(cid)
        self.send_bytes(to_srt(cues).encode("utf-8"), "application/x-subrip", f"clip_{cid}.srt")

    def exports_page(self):
        rows = "".join(
            f'<tr><td>{esc(Path(e["path"]).name)}</td><td>{esc({"done": "готово", "failed": "ошибка", "running": "идёт", "cancelled": "отменён"}.get(e["status"], e["status"]))}</td>'
            f'<td>{"" if e["duration"] is None else f"{e["duration"]:.2f} с"}</td><td>{e["width"] or ""}x{e["height"] or ""}</td>'
            f'<td>{f"<a href=/media/export/{e["id"]}>открыть</a>" if e["status"] == "done" else esc((e["error"] or "")[:200])}</td>'
            f'<td><a href="/candidate/{e["candidate_id"]}">кандидат</a></td></tr>' for e in self.app.exports())
        body = (f'<h1>Экспорт</h1><section><p>Папка: {esc(self.app.cfg.output_dir)}</p>'
                f'<form method="post" action="/open-output"><button>Открыть папку</button></form></section>'
                f'<section>{f"<table>{rows}</table>" if rows else "<p class=muted>Экспортов ещё нет.</p>"}</section>')
        self.send_html(page("Экспорт", body, self.query.get("msg", ""), self.query.get("err", "")))

    def open_output(self):
        folder = self.app.cfg.output_dir
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.redirect(urlparse(self.headers.get("Referer") or "/exports").path, msg=f"Открываю папку: {folder}")
        except OSError:
            self.redirect("/exports", err=f"Не удалось открыть папку автоматически. Путь: {folder}")

    def job_json(self, jid: int):
        j = self.app.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if j is None:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        self.send_json({"id": j["id"], "status": j["status"], "status_ru": JOB_RU.get(j["status"]),
                        "active": j["status"] in ("queued", "running"), "progress": j["progress"],
                        "message": j["message"], "error": j["error"]})

    def job_cancel(self, jid: int):
        ok = self.app.jobs.cancel(jid)
        back = urlparse(self.headers.get("Referer") or "/").path
        self.redirect(back, msg="Отмена отправлена" if ok else "Задача уже завершена")

    def media_source(self, sid: int):
        s = self.app.source(sid)
        if s is None:
            return self.not_found()
        self._serve_file(Path(s["path"]))

    def media_export(self, eid: int):
        e = self.app.db.one("SELECT * FROM exports WHERE id=? AND status='done'", (eid,))
        if e is None:
            return self.not_found()
        self._serve_file(Path(e["path"]))

    def _serve_file(self, path: Path):
        if not path.is_file():
            return self.not_found("Файл недоступен")
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = HTTPStatus.OK
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


ROUTES = {
    "GET": [
        (r"/", "home"), (r"/source/(\d+)", "source_page"), (r"/source/(\d+)/evaluation", "source_evaluation"),
        (r"/candidate/(\d+)", "candidate_page"), (r"/candidate/(\d+)/preview\.jpg", "candidate_preview"),
        (r"/candidate/(\d+)/clip\.srt", "candidate_srt"), (r"/exports", "exports_page"),
        (r"/api/job/(\d+)", "job_json"), (r"/media/source/(\d+)", "media_source"), (r"/media/export/(\d+)", "media_export"),
    ],
    "POST": [
        (r"/import", "do_import"), (r"/source/(\d+)/analyze", "source_analyze"), (r"/source/(\d+)/meta", "source_meta"),
        (r"/source/(\d+)/export", "source_export"), (r"/source/(\d+)/mark", "source_mark"),
        (r"/candidate/(\d+)/edit", "candidate_edit"), (r"/candidate/(\d+)/reset", "candidate_reset"),
        (r"/candidate/(\d+)/review", "candidate_review"), (r"/candidate/(\d+)/layout", "candidate_layout"),
        (r"/candidate/(\d+)/export", "candidate_export"), (r"/job/(\d+)/cancel", "job_cancel"),
        (r"/open-output", "open_output"),
    ],
}


def parse_time(text: str) -> float:
    """Accept seconds ('75.5', '75,5') or [h:]mm:ss(.ms)."""
    t = (text or "").strip().replace(",", ".")
    if not t:
        raise ValueError("Укажите время")
    try:
        parts = [float(p) for p in t.split(":")]
    except ValueError:
        raise ValueError(f"Не понимаю время «{text}». Пример: 75.5 или 1:15.5") from None
    value = 0.0
    for p in parts:
        value = value * 60 + p
    if value < 0:
        raise ValueError("Время не может быть отрицательным")
    return value


def make_server(app: App, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("Сервер можно запускать только на 127.0.0.1")
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def serve(app: App, port: int = 8765, open_browser: bool = True) -> None:
    server = make_server(app, port=port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"GTA6 clips запущен: {url}")
    print("Остановить: Ctrl+C в этом окне.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка…")
    finally:
        server.server_close()
        app.close()
