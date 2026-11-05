"""osu! overlay server.

Polls the osu! API v2 for whichever players are open, serves overlay.html,
and pushes rank and recent-score events over a WebSocket. The live panel
connects to tosu on the viewer's own PC, so a shared server still shows
each player their own map. The demo page downloads one map cover and one
avatar the first time it is opened, then reuses those files.

  OBS browser source (1920x1080, transparent background):
      http://localhost:8727/YourName
      http://localhost:8727/YourName/taiko

  Modes: osu (standard), taiko, fruits (catch), mania.
  Animation preview: http://localhost:8727/demo

  A public https URL works the same way. Inside an OBS browser source the
  live map still comes from tosu on that computer. A normal browser tab on
  https shows rank and scores only. Add ?live=0 to hide the map panel.

Install:  pip install -r requirements.txt
Run:      python osu_server.py

Bind address and port default to 127.0.0.1:8727. Set OSU_HOST=0.0.0.0 when
you want other machines to open the overlay. Set OSU_CLIENT_SECRET in the
environment before starting; the server exits if it is missing.
"""
import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests
from aiohttp import web, WSMsgType

CLIENT_ID = os.environ.get("OSU_CLIENT_ID", "60688")
CLIENT_SECRET = os.environ.get("OSU_CLIENT_SECRET", "")
HOST = os.environ.get("OSU_HOST", "127.0.0.1")
PORT = int(os.environ.get("OSU_PORT", "8727"))
POLL_SECONDS = 12

API = "https://osu.ppy.sh/api/v2"
HERE = Path(__file__).parent
CACHE = HERE / "cache"
MODES = {"osu": "osu", "taiko": "taiko", "fruits": "fruits", "catch": "fruits", "mania": "mania"}
USER_RE = re.compile(r"^[A-Za-z0-9_\[\] -]{2,15}$")
IDLE_GRACE = 45

trackers = {}
trackers_lock = asyncio.Lock()
demo_lock = asyncio.Lock()


class Osu:
    def __init__(self):
        self.token = None
        self.expires = 0
        self._auth_lock = threading.Lock()
        self.session = requests.Session()

    def _auth(self):
        with self._auth_lock:
            if self.token and time.time() < self.expires:
                return
            r = self.session.post(
                "https://osu.ppy.sh/oauth/token",
                json={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type": "client_credentials",
                    "scope": "public",
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            self.token = data["access_token"]
            self.expires = time.time() + data["expires_in"] - 300

    def get(self, path, **params):
        self._auth()
        r = self.session.get(
            API + path,
            headers={"Authorization": f"Bearer {self.token}"},
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()


osu = Osu()


class Tracker:
    def __init__(self, username, mode):
        self.username = username
        self.mode = mode
        self.clients = set()
        self.stats = None
        self.last = None
        self.seen = None
        self.uid = None
        self.last_client = time.time()
        self.backoff_until = 0
        self.lock = asyncio.Lock()

    @property
    def key(self):
        return (self.username.lower(), self.mode)


def norm_mode(mode):
    return MODES.get((mode or "osu").lower())


def valid_username(name):
    return bool(name) and bool(USER_RE.fullmatch(name))


async def broadcast(tracker, event):
    msg = json.dumps(event)
    dead = []
    for ws in list(tracker.clients):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        tracker.clients.discard(ws)


def mod_names(mods):
    return [m if isinstance(m, str) else m.get("acronym", "") for m in (mods or [])]


def personal_place(score, best):
    """Where this score sits in the player's top 100 pp plays.

    Returns (place, kind). place is 1-based, or None when it does not enter
    the top 100. kind is "best", "replay" (a better score on this map is
    already in the list), "outside", or "unranked".
    """
    pp = score.get("pp")
    if pp is None:
        return None, "unranked"
    beatmap_id = (score.get("beatmap") or {}).get("id")
    score_id = score.get("id")
    others = []
    for entry in best or []:
        if entry.get("id") == score_id:
            continue
        entry_map = (entry.get("beatmap") or {}).get("id")
        if beatmap_id and entry_map == beatmap_id:
            if (entry.get("pp") or 0) > pp:
                return None, "replay"
            continue
        others.append(entry.get("pp") or 0)
    place = 1 + sum(1 for other in others if other > pp)
    if place > 100:
        return None, "outside"
    return place, "best"


def build_play(score, best):
    position, kind = personal_place(score, best)
    beatmap = score.get("beatmap") or {}
    mapset = score.get("beatmapset") or {}
    covers = mapset.get("covers") or {}
    return {
        "type": "play",
        "title": mapset.get("title") or "Unknown map",
        "artist": mapset.get("artist") or "",
        "diff": beatmap.get("version") or "",
        "stars": beatmap.get("difficulty_rating"),
        "cover": covers.get("cover") or covers.get("cover@2x") or "",
        "grade": score.get("rank") or "",
        "acc": (score.get("accuracy") or 0) * 100,
        "combo": score.get("max_combo") or 0,
        "pp": score.get("pp"),
        "mods": mod_names(score.get("mods")),
        "pos": position,
        "pos_kind": kind,
    }


def snapshot(user, mode):
    st = user.get("statistics") or {}
    country = user.get("country_code") or ""
    if not country and isinstance(user.get("country"), dict):
        country = user["country"].get("code") or ""
    return {
        "username": user.get("username") or "",
        "rank": st.get("global_rank"),
        "country_rank": st.get("country_rank"),
        "country": country,
        "pp": st.get("pp") or 0,
        "acc": st.get("hit_accuracy"),
        "avatar": user.get("avatar_url") or "",
        "mode": mode,
    }


async def fetch_recent(uid, mode):
    return await asyncio.to_thread(
        osu.get,
        f"/users/{uid}/scores/recent",
        mode=mode,
        limit=10,
        include_fails=0,
    )


async def poll_user(tracker):
    recent_task = None
    if tracker.uid:
        recent_task = asyncio.create_task(fetch_recent(tracker.uid, tracker.mode))
    try:
        user = await asyncio.to_thread(
            osu.get,
            f"/users/{quote(tracker.username, safe='')}/{tracker.mode}",
            key="username",
        )
    except requests.HTTPError as e:
        if recent_task:
            recent_task.cancel()
        status = e.response.status_code if e.response is not None else 0
        if status == 404:
            tracker.backoff_until = time.time() + 120
            await broadcast(tracker, {
                "type": "error",
                "username": tracker.username,
                "message": "Player not found",
            })
            print(f"No osu! user named {tracker.username}")
            return
        raise

    tracker.backoff_until = 0
    tracker.uid = user["id"]
    if user.get("username"):
        tracker.username = user["username"]
    st = user.get("statistics") or {}
    if st.get("global_rank") is not None or tracker.stats is None:
        snap = snapshot(user, tracker.mode)
        tracker.stats = snap
        if tracker.last and snap["rank"] != tracker.last["rank"] and snap["rank"] is not None and tracker.last["rank"] is not None:
            await broadcast(tracker, {"type": "rank", "old": tracker.last["rank"], "new": snap["rank"], **snap})
            print(f"{snap['username']} [{tracker.mode}] rank {tracker.last['rank']} -> {snap['rank']}")
        elif tracker.clients and stats_changed(tracker.last, snap):
            await broadcast(tracker, {"type": "stats", **snap})
        tracker.last = snap

    recent = await recent_task if recent_task else await fetch_recent(tracker.uid, tracker.mode)
    if tracker.seen is None:
        tracker.seen = {s["id"] for s in recent}
        return

    new = [s for s in reversed(recent) if s["id"] not in tracker.seen]
    if not new:
        return
    best = await asyncio.to_thread(
        osu.get, f"/users/{tracker.uid}/scores/best", mode=tracker.mode, limit=100
    )
    for score in new:
        tracker.seen.add(score["id"])
        play = build_play(score, best)
        print(f"{tracker.username}: {play['title']} [{play['diff']}] top#{play['pos']}")
        await broadcast(tracker, play)


def stats_changed(prev, snap):
    if prev is None:
        return True
    return (
        prev["rank"] != snap["rank"]
        or prev["country_rank"] != snap["country_rank"]
        or round(prev["pp"]) != round(snap["pp"])
        or round(prev.get("acc") or 0, 2) != round(snap.get("acc") or 0, 2)
        or prev.get("avatar") != snap.get("avatar")
    )


async def refresh(tracker):
    async with tracker.lock:
        try:
            await poll_user(tracker)
        except Exception as e:
            print(f"Error ({tracker.username}/{tracker.mode}):", e)


async def poll_loop():
    while True:
        started = time.time()
        async with trackers_lock:
            current = list(trackers.values())
        for tracker in current:
            if not tracker.clients and (time.time() - tracker.last_client) > IDLE_GRACE:
                async with trackers_lock:
                    if not tracker.clients and trackers.get(tracker.key) is tracker:
                        trackers.pop(tracker.key, None)
                        print(f"Stopped watching {tracker.username} [{tracker.mode}]")
                continue
            if time.time() < tracker.backoff_until:
                continue
            await refresh(tracker)
            await asyncio.sleep(0.35)
        elapsed = time.time() - started
        await asyncio.sleep(max(1.0, POLL_SECONDS - elapsed))


def image_ext(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"GIF":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def read_demo_meta():
    path = CACHE / "demo.json"
    if not path.exists():
        return None
    meta = json.loads(path.read_text(encoding="utf-8"))
    cover = CACHE / meta.get("cover_file", "")
    avatar = CACHE / meta.get("avatar_file", "")
    if not cover.exists() or not avatar.exists():
        return None
    return meta


def download_demo():
    search = osu.get("/beatmapsets/search", query="Freedom Dive", mode=0, category="ranked")
    chosen = None
    for beatmapset in search.get("beatmapsets") or []:
        title = (beatmapset.get("title") or "").lower()
        artist = (beatmapset.get("artist") or "").lower()
        if "freedom dive" in title and artist.startswith("xi"):
            chosen = beatmapset
            break
    if chosen is None:
        sets = search.get("beatmapsets") or []
        if not sets:
            raise RuntimeError("osu! beatmap search returned nothing")
        chosen = sets[0]

    maps = [b for b in (chosen.get("beatmaps") or []) if b.get("mode") == "osu"]
    maps.sort(key=lambda b: b.get("difficulty_rating") or 0, reverse=True)
    diff = maps[0] if maps else {}
    covers = chosen.get("covers") or {}
    cover_url = covers.get("cover@2x") or covers.get("cover") or ""
    user = osu.get("/users/OnlyGalaxy/osu", key="username")
    stats = user.get("statistics") or {}
    avatar_url = user.get("avatar_url") or ""

    cover_bytes = requests.get(cover_url, timeout=20).content if cover_url else b""
    avatar_bytes = requests.get(avatar_url, timeout=20).content if avatar_url else b""
    if not cover_bytes or not avatar_bytes:
        raise RuntimeError("could not download the demo cover or avatar")

    CACHE.mkdir(parents=True, exist_ok=True)
    cover_file = "cover" + image_ext(cover_bytes)
    avatar_file = "avatar" + image_ext(avatar_bytes)
    (CACHE / cover_file).write_bytes(cover_bytes)
    (CACHE / avatar_file).write_bytes(avatar_bytes)
    meta = {
        "username": user.get("username") or "OnlyGalaxy",
        "country": user.get("country_code") or "",
        "country_rank": stats.get("country_rank"),
        "pp": stats.get("pp"),
        "acc": stats.get("hit_accuracy"),
        "rank": stats.get("global_rank"),
        "title": chosen.get("title") or "",
        "artist": chosen.get("artist") or "",
        "diff": diff.get("version") or "",
        "stars": diff.get("difficulty_rating"),
        "cover_file": cover_file,
        "avatar_file": avatar_file,
    }
    (CACHE / "demo.json").write_text(json.dumps(meta), encoding="utf-8")
    print(f"Cached demo art: {meta['artist']} - {meta['title']} [{meta['diff']}]")
    return meta


def public_demo(meta):
    if not meta:
        return {"ready": False}
    return {
        "ready": True,
        "username": meta["username"],
        "country": meta["country"],
        "country_rank": meta["country_rank"],
        "pp": meta["pp"],
        "acc": meta["acc"],
        "rank": meta["rank"],
        "title": meta["title"],
        "artist": meta["artist"],
        "diff": meta["diff"],
        "stars": meta["stars"],
        "cover": "/cache/" + meta["cover_file"],
        "avatar": "/cache/" + meta["avatar_file"],
    }


async def ensure_demo_cache():
    async with demo_lock:
        meta = read_demo_meta()
        if meta:
            return meta
        return await asyncio.to_thread(download_demo)


async def demo_data(_request):
    try:
        meta = await ensure_demo_cache()
    except Exception as e:
        print("Demo cache skipped:", e)
        return web.json_response({"ready": False})
    return web.json_response(public_demo(meta))


async def cached_image(request):
    name = request.match_info["name"]
    if not re.fullmatch(r"(cover|avatar)\.(jpg|png|webp|gif)", name):
        raise web.HTTPNotFound()
    path = CACHE / name
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def get_tracker(username, mode):
    key = (username.lower(), mode)
    async with trackers_lock:
        tracker = trackers.get(key)
        if tracker is None:
            tracker = Tracker(username, mode)
            trackers[key] = tracker
            print(f"Watching {username} [{mode}]")
    return tracker


def reject(message, status=400):
    return web.json_response({"error": message}, status=status)


async def ws_handler(request):
    username = request.match_info["username"].strip()
    mode = norm_mode(request.match_info.get("mode", "osu"))
    if not valid_username(username):
        return reject("Username must be 2–15 characters: letters, numbers, spaces, _ - [ ]")
    if not mode:
        return reject("Mode must be osu, taiko, fruits, or mania")

    tracker = await get_tracker(username, mode)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    tracker.clients.add(ws)
    tracker.last_client = time.time()
    tracker.backoff_until = 0
    if tracker.stats:
        await ws.send_str(json.dumps({"type": "stats", **tracker.stats}))
    asyncio.create_task(refresh(tracker))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        tracker.clients.discard(ws)
        tracker.last_client = time.time()
    return ws


async def index(_request):
    return web.FileResponse(HERE / "overlay.html")


async def landing(_request):
    return web.FileResponse(HERE / "home.html")


async def main():
    if not CLIENT_SECRET:
        raise SystemExit("Set OSU_CLIENT_SECRET before starting the overlay.")
    app = web.Application()
    app.router.add_get("/", landing)
    app.router.add_get("/demo", index)
    app.router.add_get("/api/demo", demo_data)
    app.router.add_get("/cache/{name}", cached_image)
    app.router.add_get("/ws/{username}", ws_handler)
    app.router.add_get("/ws/{username}/{mode}", ws_handler)
    app.router.add_get("/{username}", index)
    app.router.add_get("/{username}/{mode}", index)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    print(f"Overlay: http://{HOST}:{PORT}/YourName")
    print(f"Preview: http://{HOST}:{PORT}/demo")
    try:
        await ensure_demo_cache()
    except Exception as e:
        print("Demo cache skipped:", e)
    await poll_loop()


if __name__ == "__main__":
    asyncio.run(main())
