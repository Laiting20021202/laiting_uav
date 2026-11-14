"""Same-origin dashboard API: every image, telemetry and event route requires login."""
import asyncio
import csv
import hmac
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .auth import Auth
from .config import ROOT, read_registry
from .runtime import Runtime
from .storage import EventStore
from .flight_tracks import FlightTracks, valid_position
from .network import NetworkClient, NetworkRejected, NetworkUnavailable


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ModelBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ConfidenceBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confidence: float = Field(strict=True, ge=0.01, le=1, allow_inf_nan=False)
    track_confidence: float | None = Field(default=None, strict=True, ge=0.01, le=1, allow_inf_nan=False)
    track_ttl_s: float | None = Field(default=None, strict=True, ge=0.1, le=10, allow_inf_nan=False)


class ReconnectBody(BaseModel):
    device: str = Field(pattern="^(camera|flight|all)$")


class StopTrackBody(BaseModel):
    track_id: int = Field(gt=0)


class NetworkSwitchBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: Literal['auto', 'usb', 'wifi']
    request_id: UUID


def create_app(config, data_dir=None, users_path=None, start_workers=True):
    directory = Path(data_dir or ROOT / "data" / ("demo" if config["demo"] else "live"))
    store = EventStore(directory / "events.sqlite3", **config["storage"])
    auth = Auth(Path(users_path or ROOT / "config/users.json"), config["web"]["session_hours"],
                sessions_path=directory / "sessions.json")
    runtime = Runtime(config, store)
    tracks = (FlightTracks(directory / "flight-tracks.sqlite3", runtime.flight_snapshot,
                          stale_s=config["telemetry"]["stale_s"], demo=config["demo"],
                          min_free_mb=config["storage"]["min_free_mb"])
              if config.get("flight_tracks", {}).get("enabled", False) else None)
    origin = config["web"]["origin"]
    secure = origin.startswith("https://")
    cookie_name = "__Host-uav_session" if secure else "uav_dev_session"
    log = logging.getLogger("uav.audit")

    @asynccontextmanager
    async def lifespan(app):
        async def watchdog():
            while True:
                runtime.watchdog()
                await asyncio.sleep(5)
        watchdog_task = None
        if start_workers:
            runtime.start()
            if tracks is not None:
                tracks.start_worker()
            watchdog_task = asyncio.create_task(watchdog())
        yield
        if watchdog_task:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
        if tracks is not None:
            await asyncio.to_thread(tracks.close)
        if start_workers:
            await asyncio.to_thread(runtime.stop)
        await asyncio.to_thread(auth.close)
        store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime, app.state.auth, app.state.store = runtime, auth, store
    app.state.tracks = tracks
    app.state.network = NetworkClient()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlparse(origin).hostname, "127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if request.headers.get("origin") != origin:
                return JSONResponse({"detail": "Origin rejected"}, status_code=403)
            # API accepts small JSON bodies only, including chunked requests.
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 4096:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            request._body = bytes(raw)
        response = await call_next(request)
        # Renew both ends of the idle timeout, including authenticated camera 503s.
        # Never renew logout or an unvalidated / cross-origin request.
        token = getattr(request.state, "renew_session", None)
        if token and request.url.path != "/api/logout":
            current = await asyncio.to_thread(auth.get, token, refresh=True)
            if current:
                response.set_cookie(cookie_name, token, max_age=int(auth.lifetime),
                                    httponly=True, secure=secure, samesite="strict", path="/")
        response.headers.update({"Cache-Control": "no-store, private, max-age=0", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data: https://tile.openstreetmap.org; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def session(request, csrf=False):
        current = auth.get(request.cookies.get(cookie_name))
        if not current:
            raise HTTPException(401, "Login required")
        if csrf and not hmac.compare_digest(current["csrf"], request.headers.get("x-csrf-token", "")):
            raise HTTPException(403, "CSRF token rejected")
        request.state.renew_session = request.cookies.get(cookie_name)
        return current

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get('/ros2')
    def ros_index(request: Request):
        if not auth.get(request.cookies.get(cookie_name)):
            return RedirectResponse('/login',303)
        return RedirectResponse('/ros2/',307)

    def ros_forward(request, path, method):
        session(request, csrf=method == 'POST')
        from .ros_proxy import UnixHTTP
        conn = UnixHTTP(os.getenv('UAV_ROS_CONTROL', str(ROOT/'data/ros2/control/http.sock')))
        try:
            conn.request(method,'/'+path)
            upstream = conn.getresponse()
        except OSError as exc:
            conn.close()
            raise HTTPException(503,'ROS Docker service unavailable; check uav-ros2.service') from exc
        headers = {k:v for k,v in upstream.getheaders()
                   if k.lower() in ('content-type','content-length','content-disposition')}
        def chunks():
            try:
                while chunk := upstream.read(256*1024):
                    yield chunk
            finally:
                conn.close()
        return StreamingResponse(chunks(),status_code=upstream.status,headers=headers)

    @app.get('/ros2/{path:path}')
    def ros_get(request: Request, path: str):
        if path == '' and not auth.get(request.cookies.get(cookie_name)):
            return RedirectResponse('/login',303)
        if path not in ('','app.js','style.css','api/status','api/preview') and not path.startswith('api/download/'):
            raise HTTPException(404)
        return ros_forward(request,path,'GET')

    @app.post('/ros2/api/{action}')
    def ros_post(request: Request, action: str):
        if action not in ('start','stop'):
            raise HTTPException(404)
        return ros_forward(request,'api/'+action,'POST')

    @app.get("/")
    def index(request: Request):
        if not auth.get(request.cookies.get(cookie_name)):
            return RedirectResponse("/login", 303)
        request.state.renew_session = request.cookies.get(cookie_name)
        return FileResponse(ROOT / "uav/web/index.html")

    @app.get("/login")
    def login_page():
        return FileResponse(ROOT / "uav/web/login.html")

    @app.get("/assets/{name}")
    def asset(name: str):
        if name not in ("app.js", "login.js", "style.css", "flight-map.js", "network.js"):
            raise HTTPException(404)
        return FileResponse(ROOT / "uav/web" / name)

    @app.get("/assets/vendor/leaflet/{name}")
    def leaflet_asset(name: str):
        if name not in ("leaflet.js", "leaflet.css", "LICENSE"):
            raise HTTPException(404)
        return FileResponse(ROOT / "uav/web/vendor/leaflet" / name)

    @app.post("/api/login")
    def login(body: LoginBody, request: Request):
        # cloudflared is the only public ingress and rewrites CF-Connecting-IP.
        ip = request.client.host if request.client else "unknown"
        if ip in ("127.0.0.1", "::1"):
            ip = request.headers.get("cf-connecting-ip", ip)[:64]
        token, error = auth.login(body.username, body.password, ip)
        if error:
            log.info("Login rejected (%s)", error)
            raise HTTPException(429 if error == "limited" else 401, "請稍後再試" if error == "limited" else "帳號或密碼錯誤")
        log.info("Login accepted for %s", body.username)
        response = JSONResponse({"ok": True})
        response.set_cookie(cookie_name, token, max_age=int(auth.lifetime), httponly=True, secure=secure, samesite="strict", path="/")
        return response

    @app.get("/api/me")
    def me(request: Request):
        s = session(request)
        return {"username": s["username"], "csrf": s["csrf"]}

    @app.post("/api/logout")
    def logout(request: Request):
        session(request, csrf=True)
        auth.logout(request.cookies.get(cookie_name, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name, path="/", secure=secure, httponly=True, samesite="strict")
        return response

    @app.get("/api/status")
    def status(request: Request):
        session(request)
        state = runtime.snapshot()
        state["flight_position_valid"] = valid_position(state["flight"], state["telemetry_stale_s"])
        state["flight_tracks_enabled"] = tracks is not None
        return state

    @app.post("/api/detection/confidence")
    def set_confidence(body: ConfidenceBody, request: Request):
        current = session(request, csrf=True)
        try:
            confidence = runtime.set_confidence(body.confidence, track_confidence=body.track_confidence,
                                                track_ttl_s=body.track_ttl_s)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            log.exception("Detection confidence save failed")
            raise HTTPException(503, "信心值設定無法儲存，請重試") from exc
        log.info("Detection confidence changed by %s: %.2f", current["username"], confidence)
        result = {"confidence": confidence, "persistent": True}
        if body.track_confidence is not None:
            result["track_confidence"] = body.track_confidence
        if body.track_ttl_s is not None:
            result["track_ttl_s"] = body.track_ttl_s
        return result

    @app.get('/api/network')
    def network_status(request: Request):
        session(request)
        return app.state.network.status()

    @app.post('/api/network/switch')
    def network_switch(body: NetworkSwitchBody, request: Request):
        current = session(request, csrf=True)
        try:
            result = app.state.network.switch(body.target, body.request_id)
        except NetworkRejected as error:
            raise HTTPException(409, str(error)) from error
        except NetworkUnavailable as error:
            raise HTTPException(503, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        log.info('4G path switch requested by %s: %s (%s)',
                 current['username'], body.target, body.request_id)
        return JSONResponse(result, status_code=202)

    @app.get("/api/flight-tracks")
    def flight_tracks(request: Request, track_id: int = 0, after: int = 0):
        session(request)
        if tracks is None:
            return {"enabled": False, "active": None, "latest": None, "track": None,
                    "points": [], "has_more": False, "error": None}
        return tracks.snapshot(track_id, after)

    def require_tracks():
        if tracks is None:
            raise HTTPException(409, "飛行路線紀錄已停用，目前僅接收即時飛控資料")

    @app.post("/api/flight-tracks/start")
    def start_track(request: Request):
        current = session(request, csrf=True)
        require_tracks()
        try:
            result = tracks.start_recording()
        except OSError as exc:
            raise HTTPException(507, str(exc)) from exc
        log.info("Flight track recording started by %s: %s", current["username"], result["active"]["id"])
        return result

    @app.post("/api/flight-tracks/stop")
    def stop_track(body: StopTrackBody, request: Request):
        current = session(request, csrf=True)
        require_tracks()
        try:
            result = tracks.stop_recording(body.track_id)
        except KeyError as exc:
            raise HTTPException(404, "找不到此飛行軌跡") from exc
        log.info("Flight track recording stopped by %s: %s", current["username"], body.track_id)
        return result

    @app.get("/api/flight-tracks/{track_id}")
    def completed_track(track_id: int, request: Request, after: int = 0):
        session(request)
        require_tracks()
        try:
            return tracks.completed(track_id, after)
        except KeyError as exc:
            raise HTTPException(404, "找不到此飛行軌跡") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/system/reconnect")
    def reconnect(body: ReconnectBody, request: Request):
        current = session(request, csrf=True)
        try:
            runtime.reconnect(body.device)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        log.info("Device reconnect requested by %s: %s", current["username"], body.device)
        return JSONResponse({"reconnecting": body.device}, status_code=202)

    @app.post("/api/system/restart")
    def restart(request: Request):
        current = session(request, csrf=True)
        try:
            runtime.control.restart()
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception:
            log.exception("Service restart submission failed")
            raise HTTPException(503, "無法提交服務重啟，請檢查服務日誌")
        log.info("Application service restart requested by %s", current["username"])
        return JSONResponse({"restarting": True, "login_required_after_restart": False}, status_code=202)

    @app.get("/api/events")
    def events(request: Request, limit: int = 100, before: int = 0):
        session(request)
        batch = store.list(max(1, min(limit, 500)), before if before > 0 else None)
        example = store.example() if before <= 0 else None
        return {"events": ([example] if example else []) + [e for e in batch if not e.get("example")],
                "next_before": batch[-1]["id"] if batch else None, "oldest_id": store.oldest_id()}

    @app.delete("/api/events")
    def clear_events(request: Request):
        current = session(request, csrf=True)
        count = runtime.clear_events()
        log.info("Target records cleared by %s: %s records", current["username"], count)
        return {"deleted": count}

    @app.get("/api/events/{event_id}/image.jpg")
    def event_image(event_id: int, request: Request, thumbnail: bool = False, download: bool = False):
        session(request)
        jpeg = store.image(event_id, thumbnail=thumbnail and not download)
        if jpeg is None:
            raise HTTPException(404, "此紀錄沒有保存照片，或已被清除")
        disposition = "attachment" if download else "inline"
        return Response(jpeg, media_type="image/jpeg", headers={
            "Content-Disposition": f'{disposition}; filename="uav-target-{event_id}.jpg"'})

    @app.get("/api/events.csv")
    def export(request: Request):
        session(request)
        def rows():
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["id", "timestamp_utc", "demo", "model", "track_id", "target_valid", "target_latitude", "target_longitude", "reason", "target_quality", "orientation_source", "aircraft_latitude", "aircraft_longitude", "gps_fix", "confidence", "class_name", "twd97_epsg", "twd97_easting_m", "twd97_northing_m", "example", "has_image"])
            yield "\ufeff" + buffer.getvalue()
            before = None
            while True:
                if not auth.get(request.cookies.get(cookie_name)):
                    return
                batch = store.list(500, before)
                if not batch:
                    break
                buffer.seek(0)
                buffer.truncate(0)
                for e in batch:
                    g, f, d = e["target_gps"], e["aircraft"], e["detection"]
                    values = [e["id"], e["timestamp"], e["demo"], e["model"], d["track_id"], g["valid"], g.get("latitude"), g.get("longitude"), g.get("reason", ""), g.get("quality", ""), g.get("orientation_source", ""), f.get("latitude"), f.get("longitude"), f.get("fix_type"), d["confidence"]]
                    twd = g.get("twd97", {}) if g.get("valid") else {}
                    values.extend([d["class_name"], twd.get("epsg"), twd.get("easting_m"), twd.get("northing_m"), e.get("example", False), e["has_image"]])
                    writer.writerow(["'"+v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for v in values])
                yield buffer.getvalue()
                before = batch[-1]["id"]
        return StreamingResponse(rows(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="uav-targets.csv"'})

    @app.get("/api/models")
    def models(request: Request):
        session(request)
        try:
            return {"models": read_registry(), "active": runtime.snapshot()["model"],
                    "default": config["detection"]["model"]}
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "模型清單無法讀取，請檢查 models/ 內的 registry.json")

    @app.post("/api/models/select")
    def select_model(body: ModelBody, request: Request):
        current = session(request, csrf=True)
        try:
            runtime.switch_model(body.name)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, "模型不存在、設定無效，或目前為示範模式") from exc
        log.info("Model switch requested by %s: %s", current["username"], body.name)
        return JSONResponse({"loading": body.name, "persistent": False}, status_code=202)

    @app.get("/api/frame.jpg")
    def frame(request: Request):
        session(request)
        jpeg, seq, at = runtime.frame()
        if jpeg is None or time.monotonic()-at > 3:
            raise HTTPException(503, "No fresh camera frame")
        return Response(jpeg, media_type="image/jpeg", headers={"X-Frame-Sequence": str(seq)})

    @app.get("/api/video.mjpg")
    async def video(request: Request):
        session(request)
        async def stream():
            last = -1
            while not await request.is_disconnected():
                if not auth.get(request.cookies.get(cookie_name)):
                    break
                jpeg, seq, at = runtime.frame()
                if jpeg and seq != last and time.monotonic()-at < 3:
                    last = seq
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
                await asyncio.sleep(1/config["preview"]["fps"])
        return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"X-Accel-Buffering": "no"})

    return app
