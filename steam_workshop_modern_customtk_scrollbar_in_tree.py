
# -*- coding: utf-8 -*-
"""
UI 版本说明：
- 已去除四个统计卡片区域，把空间还给下载列表与运行日志。
- 下载列表默认字体改为 22 号，支持用户继续自定义。
- 修复上一版 Treeview height=1 导致下载列表一次只显示一行的问题。
- 下载列表改为合理固定可视行数 + 滚轮逐行滚动，既能显示多行也能滚到底。
- 下载列表滚动条改为 CustomTkinter 风格，视觉效果与运行日志区域一致。
- 下载列表滚动条直接挂到 Treeview 内部右边缘，不再作为 table 外部独立栏。
- 保留黑色 / 白色主题切换，以及原有下载、解析、CM/CDN 测试功能。

Steam Workshop Native DepotDownloader GUI
- 自主解析 Steam Workshop 链接 / ID / 合集
- 使用 DepotDownloader 原生命令下载
- 支持 CM/CDN 候选自动更新与测速
- 分离“更新列表并测试”和“仅测试”
说明：
  CM/CDN 测速用于选择网络策略与 CellID 参考。
  DepotDownloader 能直接使用的公开加速参数主要是 -cellid 与 -max-downloads；
  CDN 域名不通过伪造令牌或强制 host 的方式注入下载流程。
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception as exc:
    raise SystemExit(f"Tkinter unavailable: {exc}")

try:
    import requests
except ImportError:
    raise SystemExit("缺少 requests 库。请先执行：pip install requests")

APP_NAME = "Steam Workshop Native Downloader - Stop & Ordered Folders"
USER_AGENT = "Mozilla/5.0 SteamWorkshopNativeDownloader/1.0"
API_BASE = "https://api.steampowered.com/ISteamRemoteStorage"
PUBLISHED_DETAILS_API = f"{API_BASE}/GetPublishedFileDetails/v1/"
COLLECTION_DETAILS_API = f"{API_BASE}/GetCollectionDetails/v1/"
DEFAULT_TIMEOUT = 25
DEFAULT_RETRIES = 2
CM_ENDPOINTS = [
    "https://api.steampowered.com/ISteamDirectory/GetCMList/v1/?cellid=0&maxcount=200",
    "https://api.steampowered.com/ISteamDirectory/GetCMList/v0001/?cellid=0&maxcount=200",
]
CDN_ENDPOINTS = [
    "https://api.steampowered.com/ISteamDirectory/GetSteamPipeDomains/v1/",
    "https://api.steampowered.com/ISteamDirectory/GetSteamPipeDomains/v0001/",
]

FALLBACK_CM = [
    {"host": "cmp1-hkg1.steamserver.net", "port": 27017, "cellid": None, "label": "Hong Kong 1"},
    {"host": "cmp2-hkg1.steamserver.net", "port": 27018, "cellid": None, "label": "Hong Kong 2"},
    {"host": "cmp1-sgp1.steamserver.net", "port": 27017, "cellid": None, "label": "Singapore 1"},
    {"host": "cmp1-tyo1.steamserver.net", "port": 27017, "cellid": None, "label": "Tokyo 1"},
    {"host": "cmp1-sea1.steamserver.net", "port": 27017, "cellid": None, "label": "Seattle 1"},
]
FALLBACK_CDN = [
    {"host": "steamcontent.com", "port": 443, "cellid": None, "label": "Steam content"},
    {"host": "client-download.steampowered.com", "port": 443, "cellid": None, "label": "Steam client download"},
    {"host": "edgecast.steamstatic.com", "port": 443, "cellid": None, "label": "Steam static"},
    {"host": "dl.steam.clngaa.com", "port": 80, "cellid": None, "label": "CLNGAA CDN"},
    {"host": "cdn.akamai.steamstatic.com", "port": 443, "cellid": None, "label": "Akamai static"},
]

def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

ROOT = app_dir()
CONFIG_DIR = ROOT / "config"
CONFIG_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = CONFIG_DIR / "settings.json"
CACHE_FILE = CONFIG_DIR / "steam_servers_cache.json"

def now() -> str:
    return time.strftime("%H:%M:%S")

def safe_name(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:max_len] if name else "untitled")

def http_json(url: str, data: Optional[bytes] = None, timeout: float = 15.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read().decode(charset, "replace")
    return json.loads(raw)

@dataclass
class Settings:
    download_dir: str = str(ROOT / "downloads")
    work_dir: str = str(ROOT / "_work")
    use_acceleration: bool = True
    safe_native_mode: bool = False
    max_downloads: int = 8
    max_parallel_downloads: int = 3
    validate: bool = False
    use_lancache: bool = False
    cellid: str = ""
    selected_cm: str = ""
    selected_cdn: str = ""
    proxy: str = ""
    skip_existing: bool = True
    username: str = ""
    password: str = ""
    retry_count: int = 2
    timeout_idle_seconds: int = 300
    list_font_size: int = 22

    @classmethod
    def load(cls) -> "Settings":
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text("utf-8"))
                defaults = asdict(cls())
                defaults.update({k: v for k, v in data.items() if k in defaults})
                # 旧版默认值是 16。此版按用户要求默认迁移为 22。
                # 后续用户在设置页手动修改后，会继续保存自己的数值。
                if data.get("list_font_size", 16) == 16:
                    defaults["list_font_size"] = 22
                return cls(**defaults)
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        Path(self.download_dir).mkdir(parents=True, exist_ok=True)
        Path(self.work_dir).mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), "utf-8")

@dataclass
class WorkshopItem:
    appid: str
    publishedfileid: str
    title: str = ""
    is_collection_child: bool = False
    enabled: bool = True

def extract_publishedfile_id(text: str) -> str:
    if not text:
        raise ValueError("输入为空，无法提取 publishedfileid")
    text = text.strip()
    if text.isdigit():
        return text
    parsed = urllib.parse.urlparse(text)
    query = urllib.parse.parse_qs(parsed.query)
    if "id" in query and query["id"]:
        file_id = query["id"][0].strip()
        if file_id.isdigit():
            return file_id
    patterns = [
        r"[?&]id=(\d+)",
        r"publishedfileid=(\d+)",
        r"CommunityFilePage/(\d+)",
        r"filedetails/\?id=(\d+)",
        r"/(\d{6,})/?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    raise ValueError(f"无法从输入中提取 publishedfileid：{text}")

class SteamWorkshopClient:
    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_RETRIES,
        sleep_between_requests: float = 0.25,
        user_agent: str = USER_AGENT,
        proxies: Optional[dict] = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep_between_requests = sleep_between_requests
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        if proxies:
            self.session.proxies.update(proxies)

    def _post(self, method: str, data: dict) -> dict:
        url = f"{API_BASE}/{method}/v1/"
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(url, data=data, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                if "response" not in payload:
                    raise RuntimeError(f"Steam API 返回内容缺少 response：{payload}")
                return payload["response"]
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(0.7 * (attempt + 1))
        raise RuntimeError(f"请求 Steam API 失败：{method}，原因：{last_error}")

    def get_collection_details(self, collection_ids: Iterable[str]) -> list[dict]:
        ids = [str(x).strip() for x in collection_ids if str(x).strip()]
        if not ids:
            return []
        data = {"collectioncount": len(ids)}
        for index, file_id in enumerate(ids):
            data[f"publishedfileids[{index}]"] = file_id
        result = self._post("GetCollectionDetails", data)
        return result.get("collectiondetails", []) or []

    def get_collection_children_map(self, collection_ids: Iterable[str]) -> dict[str, list[str]]:
        details = self.get_collection_details(collection_ids)
        output: dict[str, list[str]] = {}
        for detail in details:
            parent_id = str(detail.get("publishedfileid", "")).strip()
            children = detail.get("children", []) or []
            child_ids: list[str] = []
            def sort_key(child: dict) -> int:
                try:
                    return int(child.get("sortorder", 0))
                except Exception:
                    return 0
            for child in sorted(children, key=sort_key):
                child_id = str(child.get("publishedfileid", "")).strip()
                if child_id:
                    child_ids.append(child_id)
            if parent_id:
                output[parent_id] = child_ids
        return output

    def expand_collection_ids(
        self,
        root_collection_id: str,
        recursive: bool = True,
        max_depth: int = 10,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> tuple[list[str], set[str]]:
        root_collection_id = str(root_collection_id).strip()
        ordered_ids: list[str] = []
        seen_items: set[str] = set()
        seen_collections: set[str] = {root_collection_id}
        nested_collection_ids: set[str] = set()
        current_level = [root_collection_id]
        depth = 0
        while current_level:
            if depth > max_depth:
                raise RuntimeError(f"合集嵌套层级超过 max_depth={max_depth}")
            if progress_callback:
                progress_callback(f"正在解析第 {depth + 1} 层合集，共 {len(current_level)} 个合集...")
            children_map = self.get_collection_children_map(current_level)
            next_level: list[str] = []
            for collection_id in current_level:
                for child_id in children_map.get(collection_id, []):
                    if child_id not in seen_items:
                        seen_items.add(child_id)
                        ordered_ids.append(child_id)
                    if recursive and child_id not in seen_collections:
                        seen_collections.add(child_id)
                        next_level.append(child_id)
            if not recursive:
                break
            if next_level:
                time.sleep(self.sleep_between_requests)
                probe_map = self.get_collection_children_map(next_level)
                real_nested = [cid for cid, children in probe_map.items() if children]
                nested_collection_ids.update(real_nested)
                current_level = real_nested
            else:
                current_level = []
            depth += 1
            if self.sleep_between_requests:
                time.sleep(self.sleep_between_requests)
        return ordered_ids, nested_collection_ids

    def get_published_file_details(self, file_ids: Iterable[str], batch_size: int = 100) -> list[dict]:
        ids = [str(x).strip() for x in file_ids if str(x).strip()]
        if not ids:
            return []
        all_details: list[dict] = []
        for start in range(0, len(ids), batch_size):
            batch = ids[start:start + batch_size]
            data = {"itemcount": len(batch)}
            for index, file_id in enumerate(batch):
                data[f"publishedfileids[{index}]"] = file_id
            result = self._post("GetPublishedFileDetails", data)
            all_details.extend(result.get("publishedfiledetails", []) or [])
            if self.sleep_between_requests:
                time.sleep(self.sleep_between_requests)
        return all_details

class WorkshopResolver:
    ID_RE = re.compile(r"(?:id=|/sharedfiles/filedetails/\?id=|/workshop/filedetails/\?id=)(\d+)")
    TWO_NUM_RE = re.compile(r"^\s*(\d{3,12})\s+(\d{5,24})\s*$")
    NUM_RE = re.compile(r"^\s*(\d{5,24})\s*$")

    @classmethod
    def extract_ids_from_line(cls, line: str) -> Tuple[Optional[str], Optional[str]]:
        line = line.strip()
        m2 = cls.TWO_NUM_RE.match(line)
        if m2:
            return m2.group(1), m2.group(2)
        m = cls.ID_RE.search(line)
        if m:
            return None, m.group(1)
        m = cls.NUM_RE.match(line)
        if m:
            return None, m.group(1)
        m = re.search(r"id=(\d{5,24})", line)
        if m:
            return None, m.group(1)
        return None, None

    @classmethod
    def resolve_text(cls, text: str, log=None, proxies: Optional[dict] = None) -> List[WorkshopItem]:
        raw_items: List[Tuple[Optional[str], str]] = []
        for line in text.splitlines():
            appid, pid = cls.extract_ids_from_line(line)
            if pid:
                raw_items.append((appid, pid))

        seen = set()
        uniq: List[Tuple[Optional[str], str]] = []
        for appid, pid in raw_items:
            key = (appid or "", pid)
            if key not in seen:
                seen.add(key)
                uniq.append((appid, pid))

        if not uniq:
            return []

        client = SteamWorkshopClient(timeout=30, max_retries=3, proxies=proxies)
        all_ids = [pid for _, pid in uniq]

        if log:
            log(f"正在解析 {len(uniq)} 个项目...")

        try:
            item_ids, nested_collection_ids = client.expand_collection_ids(
                ",".join(all_ids),
                recursive=True,
                progress_callback=log if log else None
            )
        except Exception as exc:
            if log:
                log(f"递归解析合集失败，尝试单条解析：{exc}")
            item_ids = all_ids
            nested_collection_ids = set()

        if not item_ids:
            item_ids = all_ids

        if log:
            log(f"正在获取 {len(item_ids)} 个子项详情...")

        try:
            details = client.get_published_file_details(item_ids)
            by_id = {str(item.get("publishedfileid")): item for item in details}
        except Exception as exc:
            if log:
                log(f"获取子项详情失败：{exc}")
            by_id = {}

        out: List[WorkshopItem] = []
        seen = set()

        for file_id in item_ids:
            if file_id in nested_collection_ids:
                continue
            d = by_id.get(file_id, {})
            app = str(d.get("consumer_app_id") or d.get("creator_app_id") or "")
            title = str(d.get("title") or "")
            key = (app, file_id)
            if key in seen:
                continue
            seen.add(key)
            if app:
                out.append(WorkshopItem(appid=app, publishedfileid=file_id, title=title, is_collection_child=True))
            elif log:
                log(f"无法解析 AppID：{file_id}。详情缺失。")

        seen_ids = set(item_ids)

        for orig_appid, pid in uniq:
            if pid in seen_ids or pid in nested_collection_ids:
                continue
            d = by_id.get(pid, {})
            app = orig_appid or str(d.get("consumer_app_id") or d.get("creator_app_id") or "")
            title = str(d.get("title") or "")
            key = (app, pid)
            if key in seen:
                continue
            seen.add(key)
            if app:
                out.append(WorkshopItem(appid=app, publishedfileid=pid, title=title, is_collection_child=False))
            elif log:
                log(f"无法解析 AppID：{pid}。可以输入格式：AppID WorkshopID")

        if log:
            log(f"解析完成，共 {len(out)} 个 Mod")

        return out

def split_host_port(addr: str, default_port: int = 27017) -> Tuple[str, int]:
    addr = str(addr).strip()
    if not addr:
        return "", default_port
    if addr.startswith("[") and "]:" in addr:
        host, p = addr.rsplit("]:", 1)
        return host.strip("[]"), int(p)
    if ":" in addr and addr.count(":") == 1:
        host, p = addr.rsplit(":", 1)
        try:
            return host, int(p)
        except ValueError:
            return addr, default_port
    return addr, default_port

class ServerManager:
    @staticmethod
    def load_cache() -> Dict[str, Any]:
        if CACHE_FILE.exists():
            try:
                return json.loads(CACHE_FILE.read_text("utf-8"))
            except Exception:
                pass
        return {"cm": FALLBACK_CM, "cdn": FALLBACK_CDN, "updated_at": 0}

    @staticmethod
    def save_cache(cm: List[Dict[str, Any]], cdn: List[Dict[str, Any]]) -> None:
        CACHE_FILE.write_text(json.dumps({
            "updated_at": int(time.time()),
            "cm": cm,
            "cdn": cdn,
        }, ensure_ascii=False, indent=2), "utf-8")

    @staticmethod
    def fetch_cm() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for url in CM_ENDPOINTS:
            try:
                js = http_json(url, timeout=15)
                response = js.get("response", js)
                servers = response.get("serverlist") or response.get("servers") or []
                for s in servers:
                    if isinstance(s, str):
                        host, port = split_host_port(s, 27017)
                        cellid = None
                        label = s
                    else:
                        addr = s.get("endpoint") or s.get("address") or s.get("server") or s.get("host") or ""
                        host, port = split_host_port(addr, int(s.get("port") or 27017))
                        cellid = s.get("cellid") or s.get("cell_id") or s.get("cell")
                        label = s.get("type") or s.get("datacenter") or s.get("realm") or addr
                    if host:
                        out.append({"host": host, "port": int(port), "cellid": cellid, "label": label})
                if out:
                    break
            except Exception:
                continue
        return ServerManager._dedupe(out or FALLBACK_CM)

    @staticmethod
    def fetch_cdn() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for url in CDN_ENDPOINTS:
            try:
                js = http_json(url, timeout=15)
                response = js.get("response", js)
                # 不同文档/接口返回名可能不同，做宽松解析
                candidates = []
                for key in ("domains", "steam_pipe_domains", "steampipe_domains", "serverlist"):
                    val = response.get(key)
                    if val:
                        candidates = val
                        break
                if isinstance(candidates, dict):
                    candidates = list(candidates.values())
                for item in candidates or []:
                    if isinstance(item, str):
                        host = item
                        port = 443
                        label = item
                        cellid = None
                    else:
                        host = item.get("host") or item.get("domain") or item.get("vhost") or item.get("endpoint") or ""
                        port = int(item.get("port") or 443)
                        label = item.get("type") or item.get("label") or host
                        cellid = item.get("cellid") or item.get("cell_id") or item.get("cell")
                    host = str(host).strip()
                    if host:
                        out.append({"host": host, "port": port, "cellid": cellid, "label": label})
                if out:
                    break
            except Exception:
                continue
        return ServerManager._dedupe(out or FALLBACK_CDN)

    @staticmethod
    def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        out = []
        for it in items:
            host = str(it.get("host") or "").strip()
            port = int(it.get("port") or 443)
            key = (host.lower(), port)
            if host and key not in seen:
                seen.add(key)
                out.append({"host": host, "port": port, "cellid": it.get("cellid"), "label": it.get("label") or host})
        return out

    @staticmethod
    def test_one(host: str, port: int, timeout: float = 2.5) -> Optional[float]:
        start = time.perf_counter()
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return round((time.perf_counter() - start) * 1000, 1)
        except Exception:
            return None

    @staticmethod
    def test_servers(servers: List[Dict[str, Any]], default_port: int, max_workers: int = 32) -> List[Dict[str, Any]]:
        def worker(s: Dict[str, Any]) -> Dict[str, Any]:
            host = s.get("host")
            port = int(s.get("port") or default_port)
            ms = ServerManager.test_one(host, port)
            r = dict(s)
            r["port"] = port
            r["latency_ms"] = ms
            return r

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(worker, servers))
        results.sort(key=lambda x: (x.get("latency_ms") is None, x.get("latency_ms") or 999999, str(x.get("host"))))
        return results

class Downloader:
    def __init__(self, settings: Settings, log, stop_event: Optional[threading.Event] = None):
        self.settings = settings
        self.log = log
        self.stop_event = stop_event or threading.Event()
        self.processes: Dict[int, subprocess.Popen] = {}
        self._process_lock = threading.Lock()

    def stop(self) -> None:
        self.stop_event.set()
        with self._process_lock:
            procs = list(self.processes.values())
        for p in procs:
            if p.poll() is None:
                with contextlib.suppress(Exception):
                    p.terminate()
        time.sleep(0.3)
        with self._process_lock:
            procs = [p for p in self.processes.values() if p.poll() is None]
        for p in procs:
            with contextlib.suppress(Exception):
                p.kill()

    @staticmethod
    def final_folder_name(item: WorkshopItem, order: int) -> str:
        # 目录名格式：001_3701087103_Mod标题
        title = safe_name(item.title, 90)
        base = f"{int(order):03d}_{item.publishedfileid}"
        return safe_name(f"{base}_{title}" if title else base, 140)

    def depot_exe(self) -> Optional[Path]:
        # onedir 模式：直接从 exe 同目录读取
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).parent
            # 检查 _internal 目录（onedir 模式常见结构）
            internal_dir = exe_dir / "_internal"
            
            for cand in [
                internal_dir / "tools" / "depotdownloader" / "DepotDownloader.exe",
                exe_dir / "tools" / "depotdownloader" / "DepotDownloader.exe",
                internal_dir / "DepotDownloader.exe",
                exe_dir / "DepotDownloader.exe",
            ]:
                if cand.exists():
                    return cand
        
        # 开发环境检查
        for cand in [
            ROOT / "DepotDownloader.exe",
            ROOT / "tools" / "DepotDownloader.exe",
            ROOT / "tools" / "depotdownloader" / "DepotDownloader.exe",
        ]:
            if cand.exists():
                return cand
        
        found = shutil.which("DepotDownloader.exe") or shutil.which("DepotDownloader")
        return Path(found) if found else None

    def build_cmd(self, appid: str, pubfile: str) -> List[str]:
        exe = self.depot_exe()
        if not exe:
            raise FileNotFoundError("未找到 DepotDownloader.exe。请在设置里选择可用的 DepotDownloader.exe。")
        cmd = [str(exe), "-app", str(appid), "-pubfile", str(pubfile)]

        # 原生安全模式：完全模拟手动命令，避免额外参数影响结果
        if self.settings.safe_native_mode:
            return cmd

        if self.settings.username.strip():
            cmd += ["-username", self.settings.username.strip()]
            if self.settings.password:
                cmd += ["-password", self.settings.password]

        if self.settings.use_acceleration:
            cellid = self.settings.cellid.strip()
            if re.fullmatch(r"\d+", cellid):
                cmd += ["-cellid", cellid]
            md = max(1, min(64, int(self.settings.max_downloads or 8)))
            cmd += ["-max-downloads", str(md)]
            if self.settings.use_lancache:
                cmd.append("-use-lancache")

        if self.settings.validate:
            cmd.append("-validate")
        return cmd

    def run_item(self, item: WorkshopItem, order: int, proc_id: int = 0) -> Optional[bool]:
        if self.stop_event.is_set():
            self.log(f"[STOP] 已停止，跳过 {item.publishedfileid}")
            return None
        out_root = Path(self.settings.download_dir)
        work_root = Path(self.settings.work_dir)
        final_dir = out_root / item.appid / self.final_folder_name(item, order)
        if self.settings.skip_existing and self._has_usable_files(final_dir):
            self.log(f"[SKIP] {item.publishedfileid} 已存在：{final_dir}")
            return True

        out_root.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)

        for attempt in range(1, int(self.settings.retry_count) + 2):
            if self.stop_event.is_set():
                self.log(f"[STOP] 已停止下载：{item.publishedfileid}")
                return None
            tmp = Path(tempfile.mkdtemp(prefix=f"{item.appid}_{item.publishedfileid}_", dir=str(work_root)))
            try:
                self.log(f"D{proc_id} {item.publishedfileid} | 第 {attempt}/{int(self.settings.retry_count)+1} 次下载")
                cmd = self.build_cmd(item.appid, item.publishedfileid)
                self.log("执行：" + " ".join(f'"{c}"' if " " in c else c for c in cmd))
                ok = self._run_process(cmd, cwd=tmp, proc_id=proc_id)
                files = self._collect_depot_files(tmp)
                if ok and files:
                    self._install_files(files, final_dir)
                    self._cleanup_final(final_dir)
                    if self._has_usable_files(final_dir):
                        title = f" - {item.title}" if item.title else ""
                        self.log(f"[SUCCESS] {item.appid}/{item.publishedfileid}{title} -> {final_dir}")
                        return True
                    self.log(f"[WARN] 已下载但未发现可用文件：{item.publishedfileid}")
                else:
                    self.log(f"[WARN] DepotDownloader 未正常完成或未产生文件：{item.publishedfileid}")
            except Exception as exc:
                self.log(f"[ERROR] {item.publishedfileid} 下载异常：{exc}")
            finally:
                with contextlib.suppress(Exception):
                    shutil.rmtree(tmp)
            if self.stop_event.is_set():
                self.log(f"[STOP] 已停止下载：{item.publishedfileid}")
                return None
            if attempt <= int(self.settings.retry_count):
                time.sleep(2)
        self.log(f"[FAIL] {item.appid}/{item.publishedfileid}")
        return False

    def _run_process(self, cmd: List[str], cwd: Path, proc_id: int = 0) -> bool:
        if self.stop_event.is_set():
            return False
        p = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._process_lock:
            self.processes[proc_id] = p
        zstd_seen = 0
        try:
            assert p.stdout is not None
            for line in p.stdout:
                if self.stop_event.is_set():
                    with contextlib.suppress(Exception):
                        p.terminate()
                    time.sleep(0.3)
                    if p.poll() is None:
                        with contextlib.suppress(Exception):
                            p.kill()
                    return False
                line = line.rstrip("\r\n")
                if not line:
                    continue
                if "Zstd compressed chunks are not yet implemented" in line:
                    zstd_seen += 1
                    if zstd_seen <= 2:
                        self.log("[WARN] 当前 DepotDownloader/SteamKit 报 Zstd chunk，不会误判成功；建议使用你手动验证可用的同一份 DepotDownloader。")
                    continue
                self.log(f"D{proc_id} | " + line)
            if self.stop_event.is_set():
                return False
            try:
                return p.wait(timeout=10) == 0
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    p.kill()
                return False
        finally:
            with self._process_lock:
                self.processes.pop(proc_id, None)

    def _collect_depot_files(self, tmp: Path) -> List[Tuple[Path, Path]]:
        depots = tmp / "depots"
        files: List[Tuple[Path, Path]] = []
        if depots.exists():
            # DepotDownloader 原生命令一般是 depots/<app>/<depotid>/<files>
            for app_dir in depots.iterdir():
                if not app_dir.is_dir():
                    continue
                for depot_dir in app_dir.iterdir():
                    if depot_dir.is_dir():
                        for p in depot_dir.rglob("*"):
                            if p.is_file():
                                rel = p.relative_to(depot_dir)
                                if self._ignore_file(rel):
                                    continue
                                files.append((p, rel))
                    elif depot_dir.is_file():
                        rel = depot_dir.relative_to(app_dir)
                        if not self._ignore_file(rel):
                            files.append((depot_dir, rel))
        else:
            # 兜底：直接扫描 tmp 下的文件
            for p in tmp.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(tmp)
                    if not self._ignore_file(rel):
                        files.append((p, rel))
        return files

    def _ignore_file(self, rel: Path) -> bool:
        parts = [x.lower() for x in rel.parts]
        if any(x in (".depotdownloader", "depotdownloader") for x in parts):
            return True
        if str(rel).lower().endswith((".manifest", ".tmp", ".log")):
            return True
        return False

    def _install_files(self, files: List[Tuple[Path, Path]], final_dir: Path) -> None:
        if final_dir.exists():
            shutil.rmtree(final_dir)
        final_dir.mkdir(parents=True, exist_ok=True)
        for src, rel in files:
            dst = final_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def _cleanup_final(self, final_dir: Path) -> None:
        # 修复多套一层：final/<id>/* 或 final/<title>/* 且只有这个目录时，展开
        for _ in range(3):
            entries = [p for p in final_dir.iterdir() if p.name != ".DS_Store"] if final_dir.exists() else []
            dirs = [p for p in entries if p.is_dir()]
            files = [p for p in entries if p.is_file()]
            if len(dirs) == 1 and not files:
                inner = dirs[0]
                temp = final_dir.parent / (final_dir.name + "_flatten_tmp")
                if temp.exists():
                    shutil.rmtree(temp)
                inner.rename(temp)
                shutil.rmtree(final_dir)
                temp.rename(final_dir)
            else:
                break
        for p in list(final_dir.rglob(".DepotDownloader")):
            with contextlib.suppress(Exception):
                shutil.rmtree(p)

    def _has_usable_files(self, final_dir: Path) -> bool:
        if not final_dir.exists():
            return False
        files = [p for p in final_dir.rglob("*") if p.is_file() and p.stat().st_size > 0 and not self._ignore_file(p.relative_to(final_dir))]
        if not files:
            return False
        # jar/zip 完整性校验；不要求每个 Mod 都是 zip/jar
        for p in files:
            if p.suffix.lower() in (".jar", ".zip"):
                if not zipfile.is_zipfile(p):
                    return False
                try:
                    with zipfile.ZipFile(p) as zf:
                        if zf.testzip() is not None:
                            return False
                except Exception:
                    return False
        return True



# ---------------------------------------------------------------------------
# Modern UI layer
# ---------------------------------------------------------------------------
try:
    import customtkinter as ctk
except Exception as exc:
    raise SystemExit(
        "缺少 customtkinter。请先执行：pip install customtkinter\n"
        f"原始错误：{exc}"
    )

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")
APP_NAME = "Steam Workshop Native Downloader - Modern UI"

THEMES = {
    "dark": {
        "window": "#07080c",
        "topbar": "#090a0f",
        "sidebar": "#090c14",
        "panel": "#101420",
        "panel2": "#0c1019",
        "panel3": "#151a26",
        "input": "#0b0f18",
        "border": "#1b2231",
        "border2": "#263044",
        "text": "#ffffff",
        "sub": "#9aa3b8",
        "muted": "#5b6478",
        "primary": "#22d3ee",
        "primary_hover": "#67e8f9",
        "primary_text": "#071018",
        "success": "#10b981",
        "success_hover": "#059669",
        "danger": "#7f1d1d",
        "danger_hover": "#991b1b",
        "button": "#202636",
        "button_hover": "#2a3246",
        "active": "#ffffff",
        "active_text": "#0b0f19",
        "tree_bg": "#0b0f18",
        "tree_head": "#151a26",
        "tree_fg": "#d7dceb",
        "tree_sel": "#0e7490",
        "log_bg": "#05070d",
    },
    "light": {
        "window": "#f4f7fb",
        "topbar": "#ffffff",
        "sidebar": "#ffffff",
        "panel": "#ffffff",
        "panel2": "#f8fafc",
        "panel3": "#eef2f7",
        "input": "#ffffff",
        "border": "#dbe3ef",
        "border2": "#cbd5e1",
        "text": "#0f172a",
        "sub": "#64748b",
        "muted": "#94a3b8",
        "primary": "#2563eb",
        "primary_hover": "#1d4ed8",
        "primary_text": "#ffffff",
        "success": "#10b981",
        "success_hover": "#059669",
        "danger": "#fee2e2",
        "danger_hover": "#fecaca",
        "button": "#e2e8f0",
        "button_hover": "#cbd5e1",
        "active": "#0f172a",
        "active_text": "#ffffff",
        "tree_bg": "#ffffff",
        "tree_head": "#eef2f7",
        "tree_fg": "#0f172a",
        "tree_sel": "#2563eb",
        "log_bg": "#0f172a",
    },
}


def _font(size: int, weight: str = "normal", family: str = "Microsoft YaHei UI"):
    return ctk.CTkFont(family=family, size=size, weight=weight)


class ModernCard(ctk.CTkFrame):
    def __init__(self, master, title: str, value: str = "0", hint: str = "", colors: Optional[Dict[str, str]] = None, **kwargs):
        self.colors = colors or THEMES["dark"]
        super().__init__(master, corner_radius=26, fg_color=self.colors["panel"], border_width=1, border_color=self.colors["border"], **kwargs)
        self.icon_dot = ctk.CTkFrame(self, width=34, height=34, corner_radius=14, fg_color=self.colors["panel2"], border_width=1, border_color=self.colors["border"])
        self.icon_dot.pack(anchor="w", padx=16, pady=(16, 8))
        self.icon_dot.pack_propagate(False)
        ctk.CTkLabel(self.icon_dot, text="›", font=_font(20, "bold"), text_color=self.colors["primary"]).pack(expand=True)
        self.value_label = ctk.CTkLabel(self, text=str(value), text_color=self.colors["text"], font=_font(24, "bold"))
        self.value_label.pack(anchor="w", padx=16)
        self.title_label = ctk.CTkLabel(self, text=title, text_color=self.colors["sub"], font=_font(13))
        self.title_label.pack(anchor="w", padx=16, pady=(2, 0))
        self.hint_label = ctk.CTkLabel(self, text=hint, text_color=self.colors["muted"], font=_font(11))
        self.hint_label.pack(anchor="w", padx=16, pady=(5, 14))

    def set_value(self, value: str, hint: Optional[str] = None):
        self.value_label.configure(text=str(value))
        if hint is not None:
            self.hint_label.configure(text=hint)


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, master: "App"):
        super().__init__(master)
        self.app = master
        self.c = master.colors
        self.title("设置")
        self.geometry("880x700")
        self.minsize(780, 560)
        self.transient(master)
        self.grab_set()
        self.configure(fg_color=self.c["window"])

        self.vars: Dict[str, Any] = {}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.grid(row=0, column=0, sticky="nsew", padx=18, pady=18)
        self.scroll.grid_columnconfigure(1, weight=1)

        s = master.settings
        row = 0

        header = ctk.CTkFrame(self.scroll, fg_color=self.c["panel"], corner_radius=26, border_width=1, border_color=self.c["border"])
        header.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(header, text="设置", font=_font(24, "bold"), text_color=self.c["text"]).pack(anchor="w", padx=20, pady=(18, 5))
        ctk.CTkLabel(header, text="下载目录、原生/加速参数、CM/CDN 测试都在这里配置。", text_color=self.c["sub"], font=_font(13)).pack(anchor="w", padx=20, pady=(0, 14))
        row += 1

        def section(text: str):
            nonlocal row
            label = ctk.CTkLabel(self.scroll, text=text, font=_font(18, "bold"), text_color=self.c["text"])
            label.grid(row=row, column=0, columnspan=4, sticky="w", pady=(18, 10))
            row += 1

        def entry(name: str, text: str, value: Any, width: int = 390, browse=None, show: str = ""):
            nonlocal row
            ctk.CTkLabel(self.scroll, text=text, text_color=self.c["sub"], font=_font(13)).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=7)
            v = tk.StringVar(value=str(value))
            self.vars[name] = v
            e = ctk.CTkEntry(self.scroll, textvariable=v, width=width, show=show, fg_color=self.c["input"], border_color=self.c["border2"], text_color=self.c["text"])
            e.grid(row=row, column=1, columnspan=2, sticky="ew", pady=7)
            if browse:
                ctk.CTkButton(self.scroll, text="选择", width=72, fg_color=self.c["button"], hover_color=self.c["button_hover"], text_color=self.c["text"], command=lambda: browse(v)).grid(row=row, column=3, padx=(10, 0), pady=7)
            row += 1

        def switch(name: str, text: str, value: bool):
            nonlocal row
            v = tk.BooleanVar(value=bool(value))
            self.vars[name] = v
            sw = ctk.CTkSwitch(self.scroll, text=text, variable=v, text_color=self.c["text"], progress_color=self.c["primary"])
            sw.grid(row=row, column=0, columnspan=4, sticky="w", pady=7)
            row += 1

        def browse_file(var):
            p = filedialog.askopenfilename(
                title="选择 DepotDownloader.exe",
                filetypes=[("DepotDownloader", "DepotDownloader.exe"), ("EXE", "*.exe"), ("All", "*.*")],
            )
            if p:
                var.set(p)

        def browse_dir(var):
            p = filedialog.askdirectory(title="选择目录")
            if p:
                var.set(p)

        section("下载设置")
        entry("download_dir", "下载目录", s.download_dir, browse=browse_dir)
        entry("work_dir", "临时工作目录", s.work_dir, browse=browse_dir)
        switch("safe_native_mode", "原生安全模式：只执行 DepotDownloader -app <AppID> -pubfile <ID>", s.safe_native_mode)
        switch("skip_existing", "跳过已下载且校验通过的项目", s.skip_existing)

        section("CM / CDN 测速与加速参数")
        switch("use_acceleration", "启用加速参数：使用 CellID 与 max-downloads", s.use_acceleration)
        entry("selected_cm", "当前 CM", s.selected_cm)
        entry("selected_cdn", "当前 CDN", s.selected_cdn)
        entry("cellid", "CellID", s.cellid, width=180)
        entry("max_downloads", "max-downloads", s.max_downloads, width=180)
        entry("max_parallel_downloads", "并行下载数（1-8，默认3）", s.max_parallel_downloads, width=180)
        entry("proxy", "HTTP/HTTPS 代理（如 http://127.0.0.1:7890）", s.proxy, width=320)
        switch("validate", "DepotDownloader -validate 校验已有文件", s.validate)
        switch("use_lancache", "DepotDownloader -use-lancache，仅本地网络有 Lancache 时使用", s.use_lancache)

        tools = ctk.CTkFrame(self.scroll, fg_color=self.c["panel2"], corner_radius=22, border_width=1, border_color=self.c["border"])
        tools.grid(row=row, column=0, columnspan=4, sticky="ew", pady=12)
        ctk.CTkButton(tools, text="更新 CM/CDN 列表并测试", fg_color=self.c["primary"], hover_color=self.c["primary_hover"], text_color=self.c["primary_text"], command=self.update_and_test).pack(side="left", padx=10, pady=10)
        ctk.CTkButton(tools, text="仅测试当前列表", fg_color=self.c["button"], hover_color=self.c["button_hover"], text_color=self.c["text"], command=self.test_only).pack(side="left", padx=4, pady=10)
        ctk.CTkButton(tools, text="打开服务器列表", fg_color=self.c["button"], hover_color=self.c["button_hover"], text_color=self.c["text"], command=self.open_server_list).pack(side="left", padx=4, pady=10)
        row += 1

        section("Steam 账号，可留空匿名")
        entry("username", "Steam 用户名", s.username, width=260)
        entry("password", "Steam 密码", s.password, width=260, show="*")
        entry("retry_count", "失败重试次数", s.retry_count, width=180)
        entry("timeout_idle_seconds", "无输出超时秒数", s.timeout_idle_seconds, width=180)

        section("界面显示")
        entry("list_font_size", "下载列表字体大小（12-28，默认22）", s.list_font_size, width=180)

        footer = ctk.CTkFrame(self, fg_color=self.c["window"])
        footer.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 20))
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(footer, text="取消", fg_color=self.c["button"], hover_color=self.c["button_hover"], text_color=self.c["text"], command=self.destroy).grid(row=0, column=1, padx=8)
        ctk.CTkButton(footer, text="保存", fg_color=self.c["primary"], hover_color=self.c["primary_hover"], text_color=self.c["primary_text"], command=self.save).grid(row=0, column=2)

    def save(self):
        s = self.app.settings
        for k, v in self.vars.items():
            val = v.get()
            if hasattr(s, k):
                current = getattr(s, k)
                if isinstance(current, bool):
                    setattr(s, k, bool(val))
                elif isinstance(current, int):
                    try:
                        setattr(s, k, int(val))
                    except Exception:
                        setattr(s, k, current)
                else:
                    setattr(s, k, str(val))
        try:
            s.list_font_size = max(12, min(28, int(s.list_font_size)))
        except Exception:
            s.list_font_size = 16
        try:
            s.max_parallel_downloads = max(1, min(8, int(s.max_parallel_downloads)))
        except Exception:
            s.max_parallel_downloads = 3
        s.save()
        if hasattr(self.app, "apply_table_style"):
            self.app.apply_table_style()
        self.app.refresh_status()
        self.destroy()

    def _threaded_server_action(self, update: bool):
        self.save()
        threading.Thread(target=lambda: self.app.server_action(update=update), daemon=True).start()

    def update_and_test(self):
        self._threaded_server_action(update=True)

    def test_only(self):
        self._threaded_server_action(update=False)

    def open_server_list(self):
        ServerListDialog(self.app)


class ServerListDialog(ctk.CTkToplevel):
    def __init__(self, master: "App"):
        super().__init__(master)
        self.app = master
        self.c = master.colors
        self.title("CM/CDN 列表")
        self.geometry("980x620")
        self.configure(fg_color=self.c["window"])
        self.transient(master)

        ctk.CTkLabel(self, text="CM / CDN 候选列表", font=_font(24, "bold"), text_color=self.c["text"]).pack(anchor="w", padx=20, pady=(18, 4))
        ctk.CTkLabel(self, text="测速结果只用于诊断与 CellID 参考，不伪造 CDN 授权令牌。", text_color=self.c["sub"], font=_font(13)).pack(anchor="w", padx=20, pady=(0, 12))

        tabs = ctk.CTkTabview(self, fg_color=self.c["panel"], segmented_button_fg_color=self.c["panel2"])
        tabs.pack(fill="both", expand=True, padx=20, pady=8)
        cm_tab = tabs.add("CM")
        cdn_tab = tabs.add("CDN")
        self.cm_tree = self._make_tree(cm_tab)
        self.cdn_tree = self._make_tree(cdn_tab)

        ctk.CTkButton(self, text="关闭", command=self.destroy, fg_color=self.c["button"], hover_color=self.c["button_hover"], text_color=self.c["text"]).pack(pady=(4, 16))
        self.load()

    def _make_tree(self, frame):
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Modern.Treeview", background=self.c["tree_bg"], foreground=self.c["tree_fg"], fieldbackground=self.c["tree_bg"], borderwidth=0, rowheight=30)
        style.configure("Modern.Treeview.Heading", background=self.c["tree_head"], foreground=self.c["sub"], borderwidth=0)
        style.map("Modern.Treeview", background=[("selected", self.c["tree_sel"])], foreground=[("selected", "#ffffff")])
        cols = ("host", "port", "cellid", "latency", "label")
        wrap = ctk.CTkFrame(frame, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=10, pady=10)
        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(0, weight=1)

        tree = ttk.Treeview(wrap, columns=cols, show="headings", style="Modern.Treeview")
        for c in cols:
            tree.heading(c, text=c)
            tree.column(c, width=160 if c != "label" else 300)

        sb_fg = self.c["panel3"] if self.app.ui_theme == "light" else self.c["log_bg"]
        sb_btn = "#cbd5e1" if self.app.ui_theme == "light" else self.c["button"]
        sb_hover = "#94a3b8" if self.app.ui_theme == "light" else self.c["button_hover"]
        scroll = ctk.CTkScrollbar(
            wrap,
            orientation="vertical",
            command=tree.yview,
            width=16,
            corner_radius=10,
            fg_color=sb_fg,
            button_color=sb_btn,
            button_hover_color=sb_hover,
        )
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        # 弹窗列表滚动条同样挂到 Treeview 内部，避免额外占列。
        scroll.destroy()
        scroll = ctk.CTkScrollbar(
            tree,
            orientation="vertical",
            command=tree.yview,
            width=18,
            corner_radius=10,
            fg_color="transparent",
            button_color=sb_btn,
            button_hover_color=sb_hover,
        )
        tree.configure(yscrollcommand=scroll.set)
        scroll.place(relx=1.0, rely=0.0, x=-6, y=6, anchor="ne", relheight=0.97)
        scroll.lift()
        return tree

    def load(self):
        cache = ServerManager.load_cache()
        for tree, key in [(self.cm_tree, "cm"), (self.cdn_tree, "cdn")]:
            for i in tree.get_children():
                tree.delete(i)
            for s in cache.get(key, []):
                tree.insert("", "end", values=(s.get("host"), s.get("port"), s.get("cellid") or "", s.get("latency_ms") or "", s.get("label") or ""))


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1480x980")
        self.minsize(1240, 780)

        self.ui_theme = "dark"
        self.colors = THEMES[self.ui_theme]
        self.settings = Settings.load()
        self.items: List[WorkshopItem] = []
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.log_history: List[str] = []
        self.downloading = False
        self.stop_event = threading.Event()
        self.current_downloader: Optional[Downloader] = None

        self._build_ui()
        self.after(100, self._poll_log)
        self.refresh_status()

    def _build_ui(self):
        self.colors = THEMES[self.ui_theme]
        ctk.set_appearance_mode("dark" if self.ui_theme == "dark" else "light")
        self.configure(fg_color=self.colors["window"])
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.topbar = ctk.CTkFrame(self, height=48, corner_radius=0, fg_color=self.colors["topbar"], border_width=0)
        self.topbar.grid(row=0, column=0, sticky="ew")
        self.topbar.grid_columnconfigure(0, weight=1)
        self._build_topbar()

        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=1, column=0, sticky="nsew", padx=18, pady=18)
        self.content.grid_columnconfigure(1, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self.content, width=280, corner_radius=28, fg_color=self.colors["sidebar"], border_width=1, border_color=self.colors["border"])
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        self.sidebar.grid_propagate(False)
        self._build_sidebar()

        self.main = ctk.CTkFrame(self.content, fg_color="transparent")
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        # 去除原来的四个大统计卡片，把空间还给下载列表和日志区
        # 行 0：顶部解析区；行 1：操作按钮；行 2：下载列表；行 3：运行日志
        self.main.grid_rowconfigure(2, weight=9, minsize=560)
        self.main.grid_rowconfigure(3, weight=2, minsize=190)

        self._build_header()
        self._build_actions()
        self._build_table()
        self._build_log_panel()
        self._restore_log_text()

    def _build_topbar(self):
        left = ctk.CTkFrame(self.topbar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=14, pady=8)
        ctk.CTkButton(left, text="×", width=30, height=30, fg_color="transparent", hover_color=self.colors["panel2"], text_color=self.colors["sub"], font=_font(18), command=lambda: None).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(left, text="Steam Workshop Downloader", text_color=self.colors["text"], font=_font(18, "bold")).pack(side="left")
        ctk.CTkLabel(left, text="⌄", text_color=self.colors["sub"], font=_font(15)).pack(side="left", padx=8)

        right = ctk.CTkFrame(self.topbar, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", padx=14, pady=7)
        for sym, tip in [("⧉", "复制"), ("↥", "导出"), ("↻", "刷新"), ("⋯", "更多")]:
            ctk.CTkButton(right, text=sym, width=32, height=32, fg_color="transparent", hover_color=self.colors["panel2"], text_color=self.colors["sub"], font=_font(16), command=lambda: None).pack(side="left", padx=2)
        ctk.CTkButton(right, text=("☀  白色主题" if self.ui_theme == "dark" else "☾  黑色主题"), height=38, width=132, corner_radius=18, fg_color=self.colors["button"], hover_color=self.colors["button_hover"], text_color=self.colors["text"], command=self.toggle_theme).pack(side="left", padx=(10, 4))
        ctk.CTkButton(right, text="■  停止", height=38, width=108, corner_radius=18, fg_color=self.colors["button"], hover_color=self.colors["button_hover"], text_color=self.colors["text"], command=self.stop_download).pack(side="left")

    def _build_sidebar(self):
        ctk.CTkLabel(self.sidebar, text="Workshop Hub", font=_font(24, "bold"), text_color=self.colors["text"]).pack(anchor="w", padx=22, pady=(28, 3))
        ctk.CTkLabel(self.sidebar, text="Native DepotDownloader", font=_font(12), text_color=self.colors["sub"]).pack(anchor="w", padx=22, pady=(0, 26))

        buttons = [
            ("⇩  下载任务", self.focus_download),
            ("☷  合集解析", self.resolve),
            ("☁  CM / CDN 测试", lambda: threading.Thread(target=lambda: self.server_action(False), daemon=True).start()),
            ("▣  日志诊断", lambda: self.log_text.focus_set() if hasattr(self, "log_text") else None),
            ("⚙  设置", lambda: SettingsDialog(self)),
        ]
        for idx, (text, cmd) in enumerate(buttons):
            fg = self.colors["active"] if idx == 0 else "transparent"
            color = self.colors["active_text"] if idx == 0 else self.colors["sub"]
            hover = self.colors["button_hover"] if idx != 0 else self.colors["active"]
            ctk.CTkButton(self.sidebar, text=text, height=48, corner_radius=16, anchor="w", fg_color=fg, text_color=color, hover_color=hover, font=_font(15), command=cmd).pack(fill="x", padx=14, pady=5)

        spacer = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        spacer.pack(fill="both", expand=True)

        self.strategy_card = ctk.CTkFrame(self.sidebar, corner_radius=24, fg_color=self.colors["panel"], border_width=1, border_color=self.colors["border"])
        self.strategy_card.pack(side="bottom", fill="x", padx=14, pady=18)
        ctk.CTkLabel(self.strategy_card, text="当前策略", font=_font(17, "bold"), text_color=self.colors["text"]).pack(anchor="w", padx=16, pady=(16, 8))
        self.strategy_label = ctk.CTkLabel(self.strategy_card, text="原生安全 / 自动 CellID", text_color=self.colors["sub"], justify="left", font=_font(14))
        self.strategy_label.pack(anchor="w", padx=16)
        ctk.CTkButton(self.strategy_card, text="◌  仅测速", height=42, fg_color=self.colors["button"], hover_color=self.colors["button_hover"], text_color=self.colors["text"], command=lambda: threading.Thread(target=lambda: self.server_action(False), daemon=True).start()).pack(fill="x", padx=16, pady=16)

    def _build_header(self):
        header = ctk.CTkFrame(self.main, corner_radius=30, fg_color=self.colors["panel"], border_width=1, border_color=self.colors["border"])
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(header, text="Steam 创意工坊下载器", font=_font(28, "bold"), text_color=self.colors["text"]).grid(row=0, column=0, sticky="w", padx=24, pady=(16, 3))
        ctk.CTkLabel(header, text="链接解析、合集展开、批量队列、CM/CDN 测速与顺序目录整理", text_color=self.colors["sub"], font=_font(15)).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 14))

        top_buttons = ctk.CTkFrame(header, fg_color="transparent")
        top_buttons.grid(row=0, column=1, rowspan=2, sticky="ne", padx=24, pady=18)
        ctk.CTkButton(top_buttons, text="打开下载目录", height=38, fg_color=self.colors["button"], hover_color=self.colors["button_hover"], text_color=self.colors["text"], command=self.open_download_dir).pack(side="left", padx=5)
        ctk.CTkButton(top_buttons, text="设置", height=38, fg_color="#2563eb" if self.ui_theme == "light" else "#1d4ed8", hover_color="#1d4ed8", text_color="#ffffff", command=lambda: SettingsDialog(self)).pack(side="left", padx=5)

        input_row = ctk.CTkFrame(header, fg_color="transparent")
        input_row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=24, pady=(0, 16))
        input_row.grid_columnconfigure(0, weight=1)
        self.input_text = ctk.CTkTextbox(input_row, height=64, corner_radius=20, fg_color=self.colors["input"], border_width=1, border_color=self.colors["border2"], text_color=self.colors["text"], font=_font(16))
        self.input_text.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self.input_text.insert("1.0", "https://steamcommunity.com/sharedfiles/filedetails/?id=3725249501\n")
        ctk.CTkButton(input_row, text="⌕  解析链接", width=152, height=56, corner_radius=18, fg_color=self.colors["primary"], hover_color=self.colors["primary_hover"], text_color=self.colors["primary_text"], font=_font(16, "bold"), command=self.resolve).grid(row=0, column=1, sticky="n", padx=4)
        ctk.CTkButton(input_row, text="↻  更新并测试", width=158, height=56, corner_radius=18, fg_color=self.colors["button"], hover_color=self.colors["button_hover"], text_color=self.colors["text"], font=_font(16, "bold"), command=lambda: threading.Thread(target=lambda: self.server_action(True), daemon=True).start()).grid(row=0, column=2, sticky="n", padx=(4, 0))

    def _build_metrics(self):
        metrics = ctk.CTkFrame(self.main, fg_color="transparent")
        metrics.grid(row=1, column=0, sticky="ew", pady=10)
        for i in range(4):
            metrics.grid_columnconfigure(i, weight=1, uniform="metric")
        self.card_total = ModernCard(metrics, "列表项目", "0", "含合集子项", colors=self.colors)
        self.card_checked = ModernCard(metrics, "勾选下载", "0", "只处理已勾选", colors=self.colors)
        self.card_network = ModernCard(metrics, "测速延迟", "--", "CellID / 测速参考", colors=self.colors)
        self.card_rule = ModernCard(metrics, "目录规则", "001", "序号_ID_标题", colors=self.colors)
        for i, card in enumerate([self.card_total, self.card_checked, self.card_network, self.card_rule]):
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0 if i == 3 else 8))

    def _build_actions(self):
        panel = ctk.CTkFrame(self.main, corner_radius=26, fg_color=self.colors["panel"], border_width=1, border_color=self.colors["border"])
        panel.grid(row=1, column=0, sticky="ew", pady=(12, 10))
        panel.grid_columnconfigure(0, weight=1)

        left = ctk.CTkFrame(panel, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=16, pady=12)
        for text, cmd, danger in [
            ("删除选中", self.delete_selected_items, True),
            ("删除未勾选", self.delete_unchecked_items, False),
            ("清空列表", self.clear_items, False),
            ("全选下载", lambda: self.set_all_enabled(True), False),
            ("全不下载", lambda: self.set_all_enabled(False), False),
            ("反选", self.invert_enabled, False),
        ]:
            ctk.CTkButton(left, text=text, width=116, height=44, corner_radius=14, fg_color=self.colors["danger"] if danger else self.colors["button"], hover_color=self.colors["danger_hover"] if danger else self.colors["button_hover"], text_color=("#ffffff" if self.ui_theme == "dark" and danger else self.colors["text"]), font=_font(15, "bold" if danger else "normal"), command=cmd).pack(side="left", padx=4)

        right = ctk.CTkFrame(panel, fg_color="transparent")
        right.grid(row=0, column=1, sticky="e", padx=16, pady=12)
        self.stop_button = ctk.CTkButton(right, text="▣  停止下载", width=148, height=46, corner_radius=16, fg_color="#8b1d1d", hover_color="#a32020", font=_font(16, "bold"), command=self.stop_download, state="disabled")
        self.stop_button.pack(side="left", padx=7)
        self.start_button = ctk.CTkButton(right, text="▷  开始下载", width=148, height=46, corner_radius=16, fg_color=self.colors["success"], hover_color=self.colors["success_hover"], text_color="#ffffff", font=_font(16, "bold"), command=self.start_download)
        self.start_button.pack(side="left")

    def get_list_font_size(self) -> int:
        try:
            return max(12, min(28, int(getattr(self.settings, "list_font_size", 16))))
        except Exception:
            return 16

    def get_list_row_height(self) -> int:
        size = self.get_list_font_size()
        # 22 号字体时约 60px 行高，清晰且可一次显示多行。
        return max(46, int(size * 2.75))

    def apply_table_style(self):
        size = self.get_list_font_size()
        row_height = self.get_list_row_height()
        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "Download.Treeview",
            background=self.colors["tree_bg"],
            foreground=self.colors["tree_fg"],
            fieldbackground=self.colors["tree_bg"],
            borderwidth=0,
            rowheight=row_height,
            font=("Microsoft YaHei UI", size),
        )
        style.configure(
            "Download.Treeview.Heading",
            background=self.colors["tree_head"],
            foreground=self.colors["sub"],
            borderwidth=0,
            font=("Microsoft YaHei UI", size, "bold"),
        )
        style.map(
            "Download.Treeview",
            background=[("selected", self.colors["tree_sel"])],
            foreground=[("selected", "#ffffff")],
        )

    def _scrollbar_colors(self):
        """下载列表滚动条使用和日志区一致的暗色/浅色圆角风格。"""
        if self.ui_theme == "light":
            return {
                "fg": self.colors["panel3"],
                "button": "#cbd5e1",
                "hover": "#94a3b8",
            }
        return {
            "fg": self.colors["log_bg"],
            "button": self.colors["button"],
            "hover": self.colors["button_hover"],
        }

    def _build_table(self):
        table_frame = ctk.CTkFrame(self.main, corner_radius=30, fg_color=self.colors["panel"], border_width=1, border_color=self.colors["border"])
        table_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(1, weight=1)

        title_bar = ctk.CTkFrame(table_frame, fg_color="transparent")
        title_bar.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 8))
        title_bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(title_bar, text="下载列表", font=_font(24, "bold"), text_color=self.colors["text"]).grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar()
        ctk.CTkLabel(title_bar, textvariable=self.status_var, text_color=self.colors["sub"], font=_font(16)).grid(row=0, column=1, sticky="e")

        self.apply_table_style()

        cols = ("enabled", "order", "appid", "id", "title", "type")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="extended", style="Download.Treeview", height=8)
        for key, title, width, anchor in [
            ("enabled", "下载", 72, "center"),
            ("order", "序号", 80, "center"),
            ("appid", "AppID", 100, "center"),
            ("id", "Workshop ID", 180, "center"),
            ("title", "标题", 360, "w"),
            ("type", "类型", 150, "center"),
        ]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "title"))

        # 重要：Treeview 的 height 表示“可视行数”，不能设为 1，否则只显示一行。
        self.tree.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 20))

        # 滚动条直接作为 Treeview 的子控件覆盖在列表内部右边缘。
        # 这样不会在下载列表外部单独占一列，也不会生成右侧空白栏。
        sbc = self._scrollbar_colors()
        self.tree_scrollbar = ctk.CTkScrollbar(
            self.tree,
            orientation="vertical",
            command=self.tree.yview,
            width=18,
            corner_radius=10,
            fg_color="transparent",
            button_color=sbc["button"],
            button_hover_color=sbc["hover"],
        )
        # CustomTkinter 的 place 不允许传 height，这里只使用 relheight。
        # x 为负数表示向 Treeview 内部缩进，滚动条不会跑到列表外面。
        self.tree_scrollbar.place(relx=1.0, rely=0.0, x=-6, y=6, anchor="ne", relheight=0.97)
        self.tree_scrollbar.lift()
        self.tree.configure(yscrollcommand=self.tree_scrollbar.set)

        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<space>", self.toggle_selected_items)
        self.tree.bind("<Delete>", lambda e: self.delete_selected_items())
        for widget in (self.tree, table_frame):
            widget.bind("<MouseWheel>", self._on_tree_mousewheel, add="+")
            widget.bind("<Button-4>", self._on_tree_mousewheel, add="+")
            widget.bind("<Button-5>", self._on_tree_mousewheel, add="+")

    def _on_tree_mousewheel(self, event):
        """让下载列表在大字号和不同 Windows 缩放下都能正常滚动到底部。"""
        if not hasattr(self, "tree"):
            return None
        if getattr(event, "num", None) == 4:
            self.tree.yview_scroll(-1, "units")
            return "break"
        if getattr(event, "num", None) == 5:
            self.tree.yview_scroll(1, "units")
            return "break"
        delta = getattr(event, "delta", 0)
        if delta:
            step = -1 if delta > 0 else 1
            # 大字体时每次滚一行，避免跳过底部最后几项。
            self.tree.yview_scroll(step, "units")
            return "break"
        return None

    def _build_log_panel(self):
        log_frame = ctk.CTkFrame(self.main, corner_radius=30, fg_color=self.colors["panel"], border_width=1, border_color=self.colors["border"])
        log_frame.grid(row=3, column=0, sticky="nsew")
        log_frame.configure(height=210)
        log_frame.grid_propagate(False)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(log_frame, text="运行日志", font=_font(24, "bold"), text_color=self.colors["text"]).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 8))
        self.log_text = ctk.CTkTextbox(log_frame, height=155, corner_radius=20, fg_color=self.colors["log_bg"], border_width=1, border_color=self.colors["border"], text_color=("#cbd5e1" if self.ui_theme == "light" else "#9ca3af"), font=ctk.CTkFont(family="Microsoft YaHei UI", size=16))
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 20))

    def toggle_theme(self):
        self.ui_theme = "light" if self.ui_theme == "dark" else "dark"
        for child in self.winfo_children():
            child.destroy()
        self._build_ui()
        self._refresh_tree()
        self.refresh_status()

    def _restore_log_text(self):
        if hasattr(self, "log_text") and self.log_history:
            self.log_text.delete("1.0", "end")
            for line in self.log_history[-400:]:
                self.log_text.insert("end", line + "\n")
            self.log_text.see("end")

    def focus_download(self):
        self.input_text.focus_set()

    def refresh_status(self):
        total = len(getattr(self, "items", []))
        checked = sum(1 for it in getattr(self, "items", []) if getattr(it, "enabled", True))
        dd = "DepotDownloader 已内置"
        if hasattr(self, "status_var"):
            self.status_var.set(f"{checked}/{total} 已勾选 | {dd}")
        if hasattr(self, "card_total"):
            self.card_total.set_value(str(total), "含合集子项")
            self.card_checked.set_value(str(checked), "只处理已勾选")
            self.card_network.set_value(self.settings.cellid or "Auto", "CellID / 测速参考")
        cm = self.settings.selected_cm or "Auto"
        cdn = self.settings.selected_cdn or "Auto"
        mode = "原生安全" if self.settings.safe_native_mode else "加速参数"
        if hasattr(self, "strategy_label"):
            self.strategy_label.configure(text=f"CM：{cm}\nCDN：{cdn}\n模式：{mode}")

    def log(self, msg: str):
        line = f"[{now()}] {msg}"
        self.log_history.append(line)
        if len(self.log_history) > 1000:
            self.log_history = self.log_history[-1000:]
        self.log_queue.put(line)

    def _poll_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if hasattr(self, "log_text"):
                    self.log_text.insert("end", msg + "\n")
                    self.log_text.see("end")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def resolve(self):
        text = self.input_text.get("1.0", "end")
        self.log("开始解析...")
        def task():
            try:
                proxies = None
                proxy_url = self.settings.proxy.strip()
                if proxy_url:
                    proxies = {
                        "http": proxy_url,
                        "https": proxy_url,
                    }
                items = WorkshopResolver.resolve_text(text, log=self.log, proxies=proxies)
                self.items = items
                self.after(0, self._refresh_tree)
                self.log(f"解析完成：{len(items)} 个项目。")
            except Exception as exc:
                self.log(f"[ERROR] 解析失败：{exc}")
        threading.Thread(target=task, daemon=True).start()

    def _refresh_tree(self):
        if not hasattr(self, "tree"):
            return
        for i in self.tree.get_children():
            self.tree.delete(i)
        for idx, it in enumerate(self.items):
            order = f"{idx + 1:03d}"
            self.tree.insert(
                "",
                "end",
                iid=str(idx),
                values=("☑" if it.enabled else "☐", order, it.appid, it.publishedfileid, it.title, "合集子项" if it.is_collection_child else "项目"),
            )
        self.refresh_status()

    def on_tree_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        column = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if column == "#1" and row:
            try:
                idx = int(row)
                self.items[idx].enabled = not self.items[idx].enabled
                self._refresh_tree()
                self.tree.selection_set(row)
            except Exception:
                pass
            return "break"

    def toggle_selected_items(self, event=None):
        selected = list(self.tree.selection())
        if not selected:
            return "break"
        for iid in selected:
            try:
                idx = int(iid)
                self.items[idx].enabled = not self.items[idx].enabled
            except Exception:
                pass
        self._refresh_tree()
        for iid in selected:
            if iid in self.tree.get_children():
                self.tree.selection_add(iid)
        return "break"

    def delete_selected_items(self):
        selected = list(self.tree.selection())
        if not selected:
            self.log("未选择要删除的项目。")
            return
        indexes = []
        for iid in selected:
            try:
                indexes.append(int(iid))
            except Exception:
                pass
        for idx in sorted(set(indexes), reverse=True):
            if 0 <= idx < len(self.items):
                self.items.pop(idx)
        self._refresh_tree()
        self.log(f"已从列表删除 {len(set(indexes))} 个项目。")

    def delete_unchecked_items(self):
        before = len(self.items)
        self.items = [it for it in self.items if it.enabled]
        removed = before - len(self.items)
        self._refresh_tree()
        self.log(f"已删除未勾选项目 {removed} 个。")

    def clear_items(self):
        count = len(self.items)
        self.items = []
        self._refresh_tree()
        self.log(f"已清空列表，共 {count} 个项目。")

    def set_all_enabled(self, enabled: bool):
        for it in self.items:
            it.enabled = enabled
        self._refresh_tree()
        self.log("已设置列表项目：" + ("全选下载" if enabled else "全不下载"))

    def invert_enabled(self):
        for it in self.items:
            it.enabled = not it.enabled
        self._refresh_tree()
        self.log("已反选下载状态。")

    def server_action(self, update: bool):
        if update:
            self.log("正在更新 CM/CDN 候选列表...")
            cm = ServerManager.fetch_cm()
            cdn = ServerManager.fetch_cdn()
            ServerManager.save_cache(cm, cdn)
            self.log(f"已更新候选：CM {len(cm)} 个，CDN {len(cdn)} 个。")
        else:
            self.log("仅测试当前缓存中的 CM/CDN 候选...")
        cache = ServerManager.load_cache()
        cm = cache.get("cm") or FALLBACK_CM
        cdn = cache.get("cdn") or FALLBACK_CDN
        self.log("正在测速 CM...")
        cm_tested = ServerManager.test_servers(cm, 27017)
        self.log("正在测速 CDN...")
        cdn_tested = ServerManager.test_servers(cdn, 443)
        ServerManager.save_cache(cm_tested, cdn_tested)

        best_cm = next((x for x in cm_tested if x.get("latency_ms") is not None), None)
        best_cdn = next((x for x in cdn_tested if x.get("latency_ms") is not None), None)
        if best_cm:
            self.settings.selected_cm = f"{best_cm.get('host')}:{best_cm.get('port')}"
            if best_cm.get("cellid"):
                self.settings.cellid = str(best_cm.get("cellid"))
            self.log(f"最快 CM：{self.settings.selected_cm} {best_cm.get('latency_ms')} ms，CellID={self.settings.cellid or '自动'}")
        else:
            self.log("CM 测试无可用结果。")
        if best_cdn:
            self.settings.selected_cdn = f"{best_cdn.get('host')}:{best_cdn.get('port')}"
            self.log(f"最快 CDN：{self.settings.selected_cdn} {best_cdn.get('latency_ms')} ms")
        else:
            self.log("CDN 测试无可用结果。")
        self.settings.save()
        self.after(0, self.refresh_status)

    def stop_download(self):
        if not self.downloading:
            self.log("当前没有正在进行的下载任务。")
            return
        self.log("[STOP] 正在停止下载，请等待当前进程退出...")
        self.stop_event.set()
        dl = self.current_downloader
        if dl is not None:
            dl.stop()

    def start_download(self):
        if self.downloading:
            messagebox.showinfo(APP_NAME, "正在下载中。")
            return
        if not self.items:
            self.resolve()
            self.after(800, self.start_download_after_resolve)
            return
        self.start_download_after_resolve()

    def start_download_after_resolve(self):
        if not self.items:
            self.log("没有可下载项目。")
            return
        selected_items = [(idx + 1, it) for idx, it in enumerate(self.items) if getattr(it, "enabled", True)]
        if not selected_items:
            self.log("列表中没有勾选要下载的项目。")
            return
        self.downloading = True
        self.stop_event.clear()
        self.stop_button.configure(state="normal")
        self.start_button.configure(state="disabled")

        def task():
            ok = 0
            fail = 0
            skipped = 0
            stopped = False
            result_lock = threading.Lock()
            try:
                dl = Downloader(self.settings, self.log, self.stop_event)
                self.current_downloader = dl
                if not dl.depot_exe():
                    self.log("[ERROR] 未找到 DepotDownloader.exe，请进入设置选择。")
                    return
                
                parallel = max(1, min(8, int(getattr(self.settings, "max_parallel_downloads", 3))))
                self.log(f"准备下载 {len(selected_items)} 个已勾选项目，列表总数 {len(self.items)} 个。")
                self.log(f"并行下载数：{parallel}")
                self.log("目录命名：<下载目录>/<AppID>/001_<ModID>_<Mod标题>，序号按解析列表从上到下。")
                self.log("模式：" + ("原生安全模式" if self.settings.safe_native_mode else "加速参数模式"))
                if self.settings.use_acceleration and not self.settings.safe_native_mode:
                    self.log(f"加速参数：CellID={self.settings.cellid or '自动'}，max-downloads={self.settings.max_downloads}，CDN测速={self.settings.selected_cdn or '未选'}。")

                def download_worker(proc_id: int, order: int, item: WorkshopItem) -> int:
                    nonlocal stopped
                    if self.stop_event.is_set():
                        stopped = True
                        return 0
                    res = dl.run_item(item, order, proc_id)
                    with result_lock:
                        if res is True:
                            return 1
                        elif res is None:
                            stopped = True
                            return 0
                        else:
                            return -1

                with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
                    futures = []
                    for idx, (order, item) in enumerate(selected_items):
                        proc_id = idx % parallel + 1
                        futures.append(executor.submit(download_worker, proc_id, order, item))
                    
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            result = future.result()
                            with result_lock:
                                if result == 1:
                                    ok += 1
                                elif result == -1:
                                    fail += 1
                        except Exception as exc:
                            with result_lock:
                                fail += 1
                                self.log(f"[ERROR] 下载任务异常：{exc}")

                if stopped or self.stop_event.is_set():
                    self.log(f"[STOPPED] 已停止。成功 {ok}，失败 {fail}，未完成 {len(selected_items) - ok - fail}。")
                else:
                    skipped = len(self.items) - len(selected_items)
                    self.log(f"[DONE] 成功 {ok}，失败 {fail}，未勾选跳过 {skipped}。")
            finally:
                self.current_downloader = None
                self.downloading = False
                self.after(0, lambda: self.stop_button.configure(state="disabled"))
                self.after(0, lambda: self.start_button.configure(state="normal"))

        threading.Thread(target=task, daemon=True).start()

    def open_download_dir(self):
        p = Path(self.settings.download_dir)
        p.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])


if __name__ == "__main__":
    App().mainloop()
