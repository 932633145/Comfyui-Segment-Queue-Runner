"""
ComfyUI 分段自动队列节点 - 最终版
"""

import math, copy, json, time, os, threading, urllib.request, urllib.error, hashlib, socket
import server, folder_paths
from aiohttp import web

# ── 日志缓冲（前端弹窗读取）──────────────────────────────────────
_sqr_log_buf: dict = {}

def _sqr_log(uid, msg):
    text = "" if msg is None else str(msg)
    print(text)
    if not uid:
        return
    k = str(uid)
    buf = _sqr_log_buf.setdefault(k, [])
    lines = text.splitlines()
    if not lines:
        lines = [""]
    buf.extend(lines)
    if text.endswith("\n"):
        buf.append("")
    if len(buf) > 3000:
        _sqr_log_buf[k] = buf[-3000:]

def _sqr_log_clear(uid):
    _sqr_log_buf.pop(str(uid), None)


def _sqr_format_exc(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def _sqr_log_cv2_issue(uid, scene: str, e: Exception):
    detail = _sqr_format_exc(e)
    if isinstance(e, ModuleNotFoundError) and getattr(e, "name", "") == "cv2":
        _sqr_log(uid, f"[SQR] ✗ {scene}: {detail}")
        _sqr_log(uid, "[SQR] ✗ 未安装 cv2 / opencv-python，请安装插件 requirements.txt 中的依赖后重启 ComfyUI。")
    else:
        _sqr_log(uid, f"[SQR] ✗ {scene}: {detail}")


def calc_segments(total_frames: int, segments: int) -> list:
    per_seg = ((math.ceil(total_frames / segments) + 3) // 4) * 4 + 1
    result = []
    for i in range(segments):
        skip = i * per_seg
        if i < segments - 1:
            limit = per_seg
        else:
            remaining = total_frames - skip
            limit = ((remaining + 3) // 4) * 4 + 1
        result.append((skip, limit))
    return result


# ── 速度记录（预计时长）──
_SPEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sqr_speed.json')

def load_speed_record():
    try:
        if os.path.exists(_SPEED_FILE):
            with open(_SPEED_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return None

# ── checkpoint 断点保护 ──────────────────────────────────────────
def get_checkpoint_path(unique_id):
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(plugin_dir, f"sqr_checkpoint_{unique_id}.json")

def write_checkpoint(unique_id, data):
    try:
        with open(get_checkpoint_path(unique_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[SQR] checkpoint 写入失败: {e}")

def read_checkpoint(unique_id):
    try:
        p = get_checkpoint_path(unique_id)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def clear_checkpoint(unique_id):
    try:
        p = get_checkpoint_path(unique_id)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def _sqr_is_managed_ref_path(path: str | None, unique_id=None) -> bool:
    base = os.path.basename(str(path or ""))
    if not base:
        return False
    prefixes = ["sqr_refkeep_", "sqr_refsnap_"]
    if unique_id:
        prefixes = [f"sqr_refkeep_{unique_id}_", f"sqr_refsnap_{unique_id}_"]
    return any(base.startswith(pref) for pref in prefixes)


def _sqr_cleanup_ref_images(paths, unique_id=None, keep_paths=None):
    keep = {os.path.realpath(str(p)) for p in (keep_paths or []) if p}
    input_dir = os.path.realpath(folder_paths.get_input_directory())
    for raw in paths or []:
        p = str(raw or "").strip()
        if not p or not _sqr_is_managed_ref_path(p, unique_id=unique_id):
            continue
        real = os.path.realpath(p)
        if real in keep:
            continue
        try:
            if os.path.commonpath([real, input_dir]) != input_dir:
                continue
        except Exception:
            continue
        try:
            if os.path.exists(real):
                os.remove(real)
                print(f"[SQR] 已清理 checkpoint 参考图: {os.path.basename(real)}")
        except Exception:
            pass


def _sqr_prepare_checkpoint_ref_images(ref_images_list, unique_id=None):
    if not ref_images_list:
        return []
    input_dir = folder_paths.get_input_directory()
    os.makedirs(input_dir, exist_ok=True)
    keep_list = []
    stamp = _sqr_now_stamp()
    import shutil as _snap_shutil
    for idx, raw in enumerate(ref_images_list, start=1):
        src = _sqr_resolve_media_path(raw) or str(raw or "").strip()
        if not src:
            continue
        src_real = os.path.realpath(src)
        if _sqr_is_managed_ref_path(src_real, unique_id=unique_id) and os.path.isfile(src_real):
            keep_list.append(src_real)
            continue
        if os.path.isfile(src_real):
            keep_name = f"sqr_refkeep_{unique_id}_{stamp}_{idx:02d}_{os.path.basename(src_real)}" if unique_id else f"sqr_refkeep_{stamp}_{idx:02d}_{os.path.basename(src_real)}"
            keep_dst = os.path.join(input_dir, keep_name)
            try:
                _snap_shutil.copy2(src_real, keep_dst)
                keep_list.append(keep_dst)
            except Exception as e:
                print(f"[SQR] ⚠ 参考图持久化失败({os.path.basename(src_real)}): {e}")
                keep_list.append(src_real)
        else:
            keep_list.append(str(raw))
    return keep_list

_SQR_COMFY_HOST_CACHE = None


def _sqr_now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + f"_{int((time.time() % 1) * 1000):03d}"


def _sqr_transition_seg_from_name(fname: str):
    import re
    patterns = [
        r"^sqr_trans_[0-9_]+_seg(\d+)\.mp4$",
        r"^sqr_trans_[a-f0-9]+_seg(\d+)\.mp4$",
        r"^segment_transition_seg(\d+)\.mp4$",
    ]
    for pat in patterns:
        m = re.match(pat, fname, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def _sqr_unique_filepath(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    while True:
        cand = f"{base}_{_sqr_now_stamp()}{ext}"
        if not os.path.exists(cand):
            return cand
        time.sleep(0.002)


def _sqr_collect_comfy_hosts() -> list[str]:
    candidates = []
    seen = set()

    def add(host, port):
        if port in (None, ""):
            return
        try:
            port = int(port)
        except Exception:
            return
        host = str(host or "").strip()
        if host in ("", "0.0.0.0", "::", "[::]"):
            host = "127.0.0.1"
        if host.startswith("http://") or host.startswith("https://"):
            host = host.split("://", 1)[1]
        host = host.strip("/ ")
        key = f"{host}:{port}"
        if key not in seen:
            seen.add(key)
            candidates.append(key)

    inst = getattr(getattr(server, "PromptServer", None), "instance", None)
    if inst is not None:
        add(getattr(inst, "address", None), getattr(inst, "port", None))
        add(getattr(inst, "host", None), getattr(inst, "port", None))
        srv = getattr(inst, "server", None)
        if srv is not None:
            add(getattr(srv, "address", None), getattr(srv, "port", None))
            add(getattr(srv, "host", None), getattr(srv, "port", None))

    add(os.environ.get("COMFYUI_HOST"), os.environ.get("COMFYUI_PORT"))
    add(os.environ.get("SERVER_HOST"), os.environ.get("SERVER_PORT"))

    for port in (8188, 8000, 9000, 8080):
        add("127.0.0.1", port)
        add("localhost", port)
    return candidates


def _sqr_probe_comfy_host(host: str) -> bool:
    for ep in ("/system_stats", "/queue", "/object_info", "/features"):
        try:
            with urllib.request.urlopen(f"http://{host}{ep}", timeout=1.2) as resp:
                code = getattr(resp, "status", 200)
                if code < 500:
                    return True
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return True
        except Exception:
            continue
    return False


def _sqr_get_comfy_host(force_refresh: bool = False) -> str:
    global _SQR_COMFY_HOST_CACHE
    if _SQR_COMFY_HOST_CACHE and not force_refresh:
        return _SQR_COMFY_HOST_CACHE
    for cand in _sqr_collect_comfy_hosts():
        if _sqr_probe_comfy_host(cand):
            _SQR_COMFY_HOST_CACHE = cand
            return cand
    _SQR_COMFY_HOST_CACHE = "127.0.0.1:8188"
    return _SQR_COMFY_HOST_CACHE


def _build_safe_input_copy_name(src_path: str, unique_id=None, prefix: str = "sqr_ref") -> str:
    try:
        real = os.path.realpath(src_path)
        st = os.stat(real)
        sig_src = f"{real}|{st.st_mtime_ns}|{st.st_size}"
    except Exception:
        real = os.path.realpath(src_path)
        sig_src = real
    sig = hashlib.sha1(sig_src.encode("utf-8", errors="ignore")).hexdigest()[:12]
    base = os.path.basename(src_path)
    if unique_id:
        return f"{prefix}_{unique_id}_{sig}_{base}"
    return f"{prefix}_{sig}_{base}"




def _sqr_media_roots() -> list[str]:
    roots = []
    seen = set()
    for getter_name in ("get_input_directory", "get_output_directory", "get_temp_directory"):
        getter = getattr(folder_paths, getter_name, None)
        if not callable(getter):
            continue
        try:
            p = getter()
        except Exception:
            continue
        if not p:
            continue
        rp = os.path.realpath(str(p))
        if rp not in seen:
            seen.add(rp)
            roots.append(rp)
    return roots


def _sqr_resolve_media_path(path: str | None) -> str | None:
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return None

    if os.path.isfile(raw):
        return os.path.realpath(raw)

    try:
        ann = folder_paths.get_annotated_filepath(raw)
        if ann and os.path.isfile(ann):
            return os.path.realpath(ann)
    except Exception:
        pass

    candidates = []
    seen = set()

    def add_candidate(p):
        if not p:
            return
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            candidates.append(rp)

    if os.path.isabs(raw):
        add_candidate(raw)
    else:
        add_candidate(raw)
        base = os.path.basename(raw)
        for root in _sqr_media_roots():
            add_candidate(os.path.join(root, raw))
            if base != raw:
                add_candidate(os.path.join(root, base))

    for cand in candidates:
        if os.path.isfile(cand):
            return cand

    base = os.path.basename(raw)
    if base == raw:
        for root in _sqr_media_roots():
            try:
                for dirpath, _, files in os.walk(root):
                    if base in files:
                        return os.path.realpath(os.path.join(dirpath, base))
            except Exception:
                continue
    return None


def _sqr_copy_into_input(src_path: str, desired_name: str | None = None,
                         unique_id=None, prefix: str = "sqr_copy") -> str:
    src_real = _sqr_resolve_media_path(src_path) or os.path.realpath(str(src_path))
    if not os.path.isfile(src_real):
        raise FileNotFoundError(src_path)

    input_dir = folder_paths.get_input_directory()
    os.makedirs(input_dir, exist_ok=True)

    if os.path.realpath(os.path.dirname(src_real)) == os.path.realpath(input_dir):
        return src_real

    name = (desired_name or "").strip() or os.path.basename(src_real)
    dst = os.path.join(input_dir, name)

    try:
        if os.path.exists(dst) and os.path.samefile(src_real, dst):
            return dst
    except Exception:
        pass

    if os.path.exists(dst):
        if desired_name:
            dst = _sqr_unique_filepath(dst)
        else:
            safe_name = _build_safe_input_copy_name(src_real, unique_id=unique_id, prefix=prefix)
            dst = os.path.join(input_dir, safe_name)

    import shutil
    shutil.copy2(src_real, dst)
    return dst
def save_speed_record(total_secs, total_frames_run):
    if total_frames_run <= 0 or total_secs <= 0:
        return
    try:
        from datetime import datetime
        with open(_SPEED_FILE, 'w') as f:
            json.dump({'spf': round(total_secs / total_frames_run, 4),
                       'date': datetime.now().strftime('%Y-%m-%d %H:%M')}, f)
    except Exception:
        pass


def build_plan_text(total_frames, segments, start_from_segment, node_id, frame_rate,
                    seg_list_override=None):
    if total_frames <= 0:
        return "✗ total_frames 必须大于 0。"
    if seg_list_override is not None:
        seg_list = seg_list_override
    else:
        seg_list = calc_segments(total_frames, segments)
    start_from_segment = max(1, min(start_from_segment, len(seg_list)))
    start_idx = start_from_segment - 1
    SEP = "═" * 45
    lines = [
        f"参考视频节点：{node_id}  总帧数：{total_frames}  模式：平均分段",
        f"共 {len(seg_list)} 段，从第 {start_from_segment} 段开始",
        "",
    ]
    for i, (skip, limit) in enumerate(seg_list):
        status = "→ 执行" if i >= start_idx else "  跳过"
        audio_s = skip / frame_rate if frame_rate > 0 else 0
        lines.append(f"  第{i+1}段 skip={skip} limit={limit} 音频={audio_s:.2f}s  {status}")
    lines.append(SEP)
    lines.append("")
    speed = load_speed_record()
    frames_to_run = sum(lmt for ii, (_, lmt) in enumerate(seg_list) if ii >= start_idx)
    segs_to_run_n = len(seg_list) - start_idx
    if speed and frames_to_run > 0:
        est = speed['spf'] * frames_to_run
        est_str = f"{est/3600:.1f}h" if est >= 3600 else f"{est/60:.0f}分钟"
        spf_str = f"{speed['spf']:.1f}s/帧"
        date_str = speed['date']
        lines.append(f"预计执行 {segs_to_run_n} 段约 {est_str}（基于 {date_str} 记录的 {spf_str}，实际因分辨率/步数等可能不同）")
    return "\n".join(lines)


def find_video_combine_node(prompt: dict, combine_node_id: str) -> str | None:
    nid = combine_node_id.strip()
    if nid and nid in prompt:
        return nid
    for nid, node in prompt.items():
        if node.get("class_type") == "VHS_VideoCombine":
            inputs = node.get("inputs", {})
            if inputs.get("save_output") is True:
                return nid
    return None


def find_audio_filename(prompt: dict, node_id: str) -> str | None:
    node = prompt.get(node_id, {})
    inputs = node.get("inputs", {})
    video = inputs.get("video", "")
    if video and isinstance(video, str):
        return video
    return None


_ANIMATE_EMBEDS_CLASS_TYPES = (
    "WanVideoAnimateEmbeds",
    "WanAnimatePlus AnimateEmbeds",
)


def find_animate_embeds_node(prompt: dict) -> str | None:
    for nid, node in prompt.items():
        if node.get("class_type") in _ANIMATE_EMBEDS_CLASS_TYPES:
            return nid
    return None


_DECODE_CLASS_HINTS = (
    "WanVideoDecode",
    "WanVideoFlashVSRDecoderLoader",
)
_BAD_IMAGE_SRC_CLASSES = frozenset({
    "LoadImage",
    "LoadImageMask",
    "VHS_LoadVideo",
    "VHS_LoadVideoPath",
})


def _sqr_link_node_id(link) -> str | None:
    if isinstance(link, list) and len(link) == 2 and not isinstance(link[0], list):
        return str(link[0])
    return None


def _sqr_walk_upstream(prompt: dict, start_nid: str | None, *, max_depth: int = 32):
    """Yield (node_id, class_type) upstream from start_nid via input links."""
    if not start_nid or start_nid not in prompt:
        return
    seen = set()
    queue = [str(start_nid)]
    depth = 0
    while queue and depth < max_depth:
        depth += 1
        next_q = []
        for nid in queue:
            if nid in seen or nid not in prompt:
                continue
            seen.add(nid)
            node = prompt[nid]
            yield nid, node.get("class_type", "")
            for val in node.get("inputs", {}).values():
                up = _sqr_link_node_id(val)
                if up and up in prompt:
                    next_q.append(up)
        queue = next_q


def _sqr_is_decode_class(class_type: str | None) -> bool:
    ct = class_type or ""
    if ct in _DECODE_CLASS_HINTS:
        return True
    return "Decode" in ct and "Loader" not in ct


def _sqr_find_decode_upstream(prompt: dict, start_nid: str | None) -> str | None:
    for nid, ct in _sqr_walk_upstream(prompt, start_nid):
        if _sqr_is_decode_class(ct):
            return nid
    return None


def _sqr_resolve_image_src(prompt: dict, vc_nid: str | None, ae_nid: str | None):
    """Return (link, source_nid, class_type, note) for crop chain image input."""
    vc_link = None
    vc_src = None
    vc_ct = ""
    if vc_nid and vc_nid in prompt:
        vc_link = prompt[vc_nid].get("inputs", {}).get("images")
        vc_src = _sqr_link_node_id(vc_link)
        if vc_src and vc_src in prompt:
            vc_ct = prompt[vc_src].get("class_type", "")

    if vc_src and _sqr_is_decode_class(vc_ct):
        return [vc_src, 0], vc_src, vc_ct, "VideoCombine上游Decode"

    if vc_src and vc_ct not in _BAD_IMAGE_SRC_CLASSES:
        decode_nid = _sqr_find_decode_upstream(prompt, vc_src)
        if decode_nid:
            return [decode_nid, 0], decode_nid, prompt[decode_nid].get("class_type", ""), (
                f"沿{vc_ct}[{vc_src}]追溯到Decode"
            )

    decode_nid = _sqr_find_decode_upstream(prompt, vc_src)
    if not decode_nid and ae_nid:
        decode_nid = _sqr_find_decode_upstream(prompt, ae_nid)
    if decode_nid:
        note = "自动定位Decode"
        if vc_src and vc_ct in _BAD_IMAGE_SRC_CLASSES:
            note = f"VideoCombine误接{vc_ct}[{vc_src}]，已改接Decode"
        return [decode_nid, 0], decode_nid, prompt[decode_nid].get("class_type", ""), note

    if vc_link and vc_src:
        note = f"沿用VideoCombine上游{vc_ct}[{vc_src}]"
        if vc_ct in _BAD_IMAGE_SRC_CLASSES:
            note += "（⚠非Decode，裁切可能无效）"
        return vc_link, vc_src, vc_ct, note
    return None, None, "", "未找到图像来源"


def _sqr_wire_ref_images(wf: dict, ae_nid: str | None, ri_node_id: str | None,
                         ref_images_list, log):
    if not ref_images_list or not ae_nid or ae_nid not in wf:
        return
    target = (ri_node_id or "").strip()
    if target and target in wf:
        wf[ae_nid].setdefault("inputs", {})["ref_images"] = [target, 0]
        log(f"  ✓ ref_images → [{target}]")
        return
    ref_link = wf[ae_nid].get("inputs", {}).get("ref_images")
    ref_src = _sqr_link_node_id(ref_link)
    if ref_src:
        log(f"  ✓ ref_images 已连线 [{ref_src}]")
    else:
        log("  ⚠ 有分段参考图但 ref_images 未连线，生成可能无参考图前缀")


# Author manual uses n+3 / trim 3; load extra 6 for WAP widget alignment.
_WAP_LOAD_EXTRA = 6
_WAP_TAIL_TRIM = 3
_WAP_TRANSITION_FRAMES = 21
_WAP_REF_PREFIX_FRAMES = 4
_WAP_NUM_FRAMES_WIDGET_IDX = 2  # width, height, num_frames, ...
_WAP_FRAME_WINDOW_WIDGET_IDX = 4
# WanAnimatePlus Decode (>=20260528) strips ref_prefix_pixels in latent + pixel domain.
_WAP_DECODE_STRIPS_REF_PREFIX = True
# 临时诊断：在首段额外导出一份未裁切的 Decode 原始输出，用于校准真实剥帧数。
_SQR_DIAG_RAW_DECODE = True


def _sqr_wap_gen_frames(content_limit: int) -> int:
    """Aligned num_frames / frame_load_cap for WAP (must match pose load length)."""
    return _sqr_wap_num_frames_for_widget(content_limit)


def _sqr_wap_aligned_frames(n: int) -> int:
    return ((n - 1) // 4) * 4 + 1


def _sqr_wap_num_frames_for_widget(content_limit: int) -> int:
    """Smallest 4n+1 num_frames WAP keeps after its internal alignment, >= n+3."""
    target = content_limit + _WAP_LOAD_EXTRA
    aligned = _sqr_wap_aligned_frames(target)
    if aligned < target:
        aligned = _sqr_wap_aligned_frames(target + 4)
    return aligned


def _sqr_wap_decode_strips_transition(
    content_limit: int,
    use_transition: bool,
    frame_window_size: int,
    has_start_ref: bool,
) -> bool:
    """WanAnimatePlus Decode (>=2026-05-28) always strips canvas_expansion_px in both
    looping and non-looping modes. Even on older Decode versions where the transition
    is kept, our crop chain uses negative slicing (`-(limit+tail):-tail`) which still
    selects the correct main-generation frames from the tail. So always returning True
    is robust either way and avoids double-skipping the 21 transition frames (which
    caused ~20 frames missing at every segment boundary)."""
    return True


def _sqr_transition_constants(class_type: str | None) -> tuple[int, int, int]:
    """Return (transition_frames, trim_front, trim_back) for the AnimateEmbeds implementation."""
    if class_type == "WanAnimatePlus AnimateEmbeds":
        # 作者手动分段说明：每段读 n+3 帧，过渡 21 帧，最终裁尾 3 帧得到 n 帧。
        return _WAP_TRANSITION_FRAMES, _WAP_TRANSITION_FRAMES, _WAP_TAIL_TRIM
    # Original WanVideoAnimateEmbeds: 32 px transition, 16+16 trim.
    return 32, 16, 16


def _sqr_read_int_input(node: dict | None, key: str, default: int = 0) -> int:
    val = (node or {}).get("inputs", {}).get(key, default)
    if isinstance(val, list):
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _sqr_set_int_input(wf: dict, node_id: str, key: str, value: int, widget_index: int | None = None):
    node = wf[node_id]
    node.setdefault("inputs", {})[key] = int(value)
    wv = node.get("widgets_values")
    if wv is not None and widget_index is not None and widget_index < len(wv):
        wv[widget_index] = int(value)


def _sqr_parse_int(val, default: int = 0) -> int:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return int(val)
    try:
        return int(val)
    except (TypeError, ValueError):
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return default


def _sqr_has_ref_images(ae_node: dict | None) -> bool:
    ref = (ae_node or {}).get("inputs", {}).get("ref_images")
    return isinstance(ref, list) and len(ref) == 2


def _sqr_wap_ref_in_use(ae_node: dict | None, ref_images_list=None) -> bool:
    """Whether this segment uses reference image conditioning (needs pixel prefix skip)."""
    if ref_images_list:
        return True
    if _sqr_has_ref_images(ae_node):
        return True
    ae_in = (ae_node or {}).get("inputs", {})
    if isinstance(ae_in.get("start_ref_image"), list):
        return True
    if isinstance(ae_in.get("prefix_frames"), list):
        return True
    return False


def _sqr_wap_ref_prefix_frames(ae_node: dict | None, ref_images_list=None) -> int:
    """Pixel ref prefix length (WAP Decode strips this; SQR only logs it)."""
    if not _sqr_wap_ref_in_use(ae_node, ref_images_list):
        return 0
    ae_in = (ae_node or {}).get("inputs", {})
    if isinstance(ae_in.get("prefix_frames"), list):
        return 17
    n_refs = max(1, len(ref_images_list or []))
    return min(n_refs, 5) * 4


def _sqr_wap_decode_visible_frames(content_limit: int, use_transition: bool, has_ref: bool) -> int:
    """Approximate WanAnimatePlus Decode output length (after ref-latent drop + canvas strip)."""
    gen_n = _sqr_wap_gen_frames(content_limit)
    n = ((gen_n - 1) // 4) * 4 + 1
    if use_transition:
        n += _WAP_TRANSITION_FRAMES
        n -= (n - 1) % 4
    if has_ref:
        n += 4
    if has_ref:
        n -= 4
    if use_transition:
        n -= _WAP_TRANSITION_FRAMES
    return max(gen_n, n)


def _sqr_wap_expected_full_frames(content_limit: int, use_transition: bool, has_ref: bool) -> int:
    """After SQR crop, saved segment length should be content_limit (n)."""
    return content_limit


def _sqr_wap_crop_offsets(
    limit: int,
    use_transition: bool,
    frame_window_size: int,
    has_start_ref: bool,
    ref_in_use: bool,
    ae_node: dict | None = None,
    ref_images_list=None,
) -> tuple[int, int, int, str]:
    """Return (batch_skip, middle_len, save_len, log_note) for WAP ImageFromBatch chain."""
    gen_n = _sqr_wap_gen_frames(limit)
    widget_n = _sqr_wap_num_frames_for_widget(limit)
    ref_px_meta = _sqr_wap_ref_prefix_frames(ae_node, ref_images_list) if ref_in_use else 0
    if ref_in_use and ref_px_meta == 0:
        ref_px_meta = _WAP_REF_PREFIX_FRAMES
    ref_skip = 0 if _WAP_DECODE_STRIPS_REF_PREFIX else ref_px_meta
    trans = _WAP_TRANSITION_FRAMES
    tb = _WAP_TAIL_TRIM
    save_len = limit
    middle_len = save_len + tb

    trans_skip = 0
    trans_note = ""
    if use_transition:
        if _sqr_wap_decode_strips_transition(limit, True, frame_window_size, has_start_ref):
            trans_note = f"Decode剥{trans}后"
        else:
            trans_skip = trans
            trans_note = f"looping未Decode剥过渡→裁前{trans}+"

    batch_skip = ref_skip + trans_skip
    if ref_px_meta and _WAP_DECODE_STRIPS_REF_PREFIX:
        ref_note = f"WAP Decode剥参考图{ref_px_meta}帧；"
    elif ref_px_meta:
        ref_note = f"跳过参考图前缀{ref_px_meta}帧；"
    else:
        ref_note = ""
    note = (
        f"WAP：读{gen_n} widget={widget_n}"
        + (f"+过渡{trans}" if use_transition else "")
        + f"，{ref_note}{trans_note}取{middle_len}帧再存{save_len}帧(裁尾{tb})"
    )
    return batch_skip, middle_len, save_len, note


def _sqr_crop_plan(
    class_type: str | None,
    limit: int,
    use_transition: bool,
    is_last_seg: bool,
    total_segs: int,
    *,
    frame_window_size: int = 77,
    has_start_ref: bool = False,
    has_ref_images: bool = False,
    ref_images_list=None,
    ae_node: dict | None = None,
) -> tuple[int, int, str, int]:
    """Return (batch_skip, save_len, log_note, middle_len) for ImageFromBatch cut chain."""
    trans, tf, tb = _sqr_transition_constants(class_type)
    if class_type == "WanAnimatePlus AnimateEmbeds":
        ref_in_use = has_ref_images or bool(ref_images_list)
        batch_skip, middle_len, save_len, note = _sqr_wap_crop_offsets(
            limit, use_transition, frame_window_size, has_start_ref, ref_in_use,
            ae_node=ae_node, ref_images_list=ref_images_list)
        return batch_skip, save_len, note, middle_len

    total_raw = limit + (trans if use_transition else 0)
    if not use_transition:
        trim_len = total_raw - tb
        return 0, trim_len, f"不裁前，裁后{tb}帧→输出{trim_len}帧", trim_len
    if is_last_seg:
        trim_len = total_raw - tf
        return tf, trim_len, f"裁前{tf}帧，不裁后→输出{trim_len}帧", trim_len
    trim_len = total_raw - tf - tb
    return tf, trim_len, f"裁前{tf}裁后{tb}→输出{trim_len}帧", trim_len


def _sqr_pick_ref_image_idx(
    seg_global_idx: int,
    seg_list: list,
    total_frames: int,
    num_imgs: int,
) -> int:
    """Pick reference image index for a segment.

    - num_imgs >= total_segs: 1:1 mapping by global segment index, clamped to last.
      (preserves v2.4 manual-duplicate workflow where users copy images for
      precise per-segment control)
    - num_imgs <  total_segs: even distribution by frame range. Each image owns
      `total_frames / num_imgs` frames; pick the bucket where the segment's
      center frame falls.
    """
    if num_imgs <= 1:
        return 0
    total_segs = len(seg_list)
    if seg_global_idx >= total_segs:
        seg_global_idx = total_segs - 1
    if num_imgs >= total_segs:
        return min(seg_global_idx, num_imgs - 1)
    skip, limit = seg_list[seg_global_idx]
    center = skip + limit / 2.0
    if total_frames <= 0:
        return min(seg_global_idx * num_imgs // total_segs, num_imgs - 1)
    bucket = int(center * num_imgs / total_frames)
    return max(0, min(bucket, num_imgs - 1))


def queue_prompt(workflow, host=None, client_id="") -> str:
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode("utf-8")
    last_err = None
    for _host in [host or _sqr_get_comfy_host(), _sqr_get_comfy_host(force_refresh=True)]:
        try:
            req = urllib.request.Request(
                f"http://{_host}/prompt", data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())["prompt_id"]
        except Exception as e:
            last_err = e
    raise last_err


def wait_for_prompt(prompt_id, host=None, poll=5) -> bool:
    while True:
        time.sleep(poll)
        for _host in [host or _sqr_get_comfy_host(), _sqr_get_comfy_host(force_refresh=True)]:
            try:
                with urllib.request.urlopen(f"http://{_host}/history/{prompt_id}", timeout=10) as resp:
                    history = json.loads(resp.read())
                if prompt_id in history:
                    st = history[prompt_id].get("status", {})
                    if st.get("completed"):
                        return True
                    if st.get("status_str") == "error":
                        return False
                    break
            except Exception:
                continue


def _sqr_pick_video_gif(gifs: list, prefer_audio: bool = False) -> dict | None:
    """Pick VHS_VideoCombine output. With audio input, VHS writes video-only mp4 plus *-audio* mux."""
    if not gifs:
        return None
    if prefer_audio:
        for gi in gifs:
            fn = str(gi.get("filename", ""))
            if fn.endswith(".mp4") and "-audio" in fn:
                return gi
    for gi in gifs:
        fn = str(gi.get("filename", ""))
        if fn.endswith(".mp4") and "-audio" not in fn:
            return gi
    for gi in gifs:
        fn = str(gi.get("filename", ""))
        if fn.endswith(".mp4"):
            return gi
    return gifs[0]


def _sqr_find_video_by_prefix(search_dir: str, prefix: str, prefer_audio: bool = False):
    if not search_dir or not os.path.isdir(search_dir):
        return None, None
    video_only, with_audio = [], []
    for fn in os.listdir(search_dir):
        if not fn.startswith(prefix) or not fn.lower().endswith(".mp4"):
            continue
        full = os.path.join(search_dir, fn)
        if "-audio" in fn:
            with_audio.append(full)
        else:
            video_only.append(full)
    if prefer_audio:
        path = sorted(with_audio or video_only)[0] if (with_audio or video_only) else None
    else:
        path = sorted(video_only or with_audio)[0] if (video_only or with_audio) else None
    if not path:
        return None, None
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        try:
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if cap.isOpened() else None
        finally:
            cap.release()
        return path, frames
    except Exception:
        return path, None


def get_output_video_info(prompt_id, combine_node_id, host=None, logger=None, prefer_audio=False):
    last_err = None
    for _host in [host or _sqr_get_comfy_host(), _sqr_get_comfy_host(force_refresh=True)]:
        try:
            with urllib.request.urlopen(f"http://{_host}/history/{prompt_id}", timeout=10) as resp:
                history = json.loads(resp.read())
            node_out = history.get(prompt_id, {}).get("outputs", {}).get(str(combine_node_id), {})
            gifs = node_out.get("gifs", [])
            if not gifs:
                return None, None
            gi = _sqr_pick_video_gif(gifs, prefer_audio=prefer_audio)
            base_dir = folder_paths.get_output_directory() if gi.get("type") == "output" \
                       else folder_paths.get_input_directory()
            subfolder = gi.get("subfolder", "")
            video_path = os.path.join(base_dir, subfolder, gi["filename"]) if subfolder \
                         else os.path.join(base_dir, gi["filename"])
            import cv2
            cap = cv2.VideoCapture(video_path)
            try:
                frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if cap.isOpened() else None
            finally:
                cap.release()
            return video_path, frames
        except Exception as e:
            last_err = e
    msg = f"✗ 获取视频信息失败: {_sqr_format_exc(last_err)}" if last_err else "✗ 获取视频信息失败"
    if logger:
        logger(msg)
    else:
        print(f"[SQR] {msg}")
    return None, None


# ── 无损 PNG 衔接（消除段间色差）─────────────────────────────────
# 旧逻辑用上段有损 mp4(yuv420p) 的末 N 帧做 transition，RGB→YUV420→RGB 往返
# 会引入色偏，导致下段开头变色。改用 SaveImage 存无损 PNG + VHS_LoadImagesPath
# 读取，PNG 经 PIL 直接 RGB，无 YUV round-trip → 零色偏。
def _sqr_lossless_transition_enabled() -> bool:
    v = str(os.environ.get("SQR_LOSSLESS_TRANSITION", "1")).strip().lower()
    return v not in ("0", "false", "no", "off")


def _sqr_trans_frames_subfolder(run_stamp, seg_num) -> str:
    return f"sqr_tframes_{run_stamp}_seg{seg_num}"


def _sqr_trans_frames_dir(run_stamp, seg_num) -> str:
    return os.path.join(folder_paths.get_output_directory(),
                        _sqr_trans_frames_subfolder(run_stamp, seg_num))


def _sqr_count_dir_images(dir_path) -> int:
    try:
        if not dir_path or not os.path.isdir(dir_path):
            return 0
        exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
        return sum(1 for f in os.listdir(dir_path) if f.lower().endswith(exts))
    except Exception:
        return 0


def _sqr_resolve_trans_dir(path) -> str | None:
    """Resolve a PNG transition directory from an absolute path or an output subfolder name."""
    p = str(path or "").strip().strip('"').strip("'")
    if not p:
        return None
    if os.path.isdir(p):
        return os.path.realpath(p)
    cand = os.path.join(folder_paths.get_output_directory(), os.path.basename(p))
    if os.path.isdir(cand):
        return os.path.realpath(cand)
    return None


def interrupt_current(host=None):
    for _host in [host or _sqr_get_comfy_host(), _sqr_get_comfy_host(force_refresh=True)]:
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"http://{_host}/interrupt", data=b"", method="POST"), timeout=10)
            return
        except Exception:
            continue


def _sqr_read_vhs_encode_opts(prompt: dict | None, vc_nid: str | None) -> dict:
    """Read VHS_VideoCombine pix_fmt/crf so merge matches per-segment exports."""
    opts = {"pix_fmt": "yuv420p", "crf": 18}
    if not prompt or not vc_nid or vc_nid not in prompt:
        return opts
    node = prompt[vc_nid]
    inp = node.get("inputs", {})
    pix = inp.get("pix_fmt")
    if pix is not None and not isinstance(pix, list):
        pix_s = str(pix).strip()
        if pix_s:
            opts["pix_fmt"] = pix_s
    crf_val = inp.get("crf")
    if crf_val is not None and not isinstance(crf_val, list):
        opts["crf"] = _sqr_parse_int(crf_val, opts["crf"])
    wv = node.get("widgets_values")
    if isinstance(wv, dict):
        if wv.get("pix_fmt"):
            opts["pix_fmt"] = str(wv["pix_fmt"]).strip()
        if wv.get("crf") is not None:
            opts["crf"] = _sqr_parse_int(wv["crf"], opts["crf"])
    elif isinstance(wv, (list, tuple)) and len(wv) >= 6:
        if isinstance(wv[4], str) and wv[4].strip():
            opts["pix_fmt"] = wv[4].strip()
        opts["crf"] = _sqr_parse_int(wv[5], opts["crf"])
    return opts


def _sqr_probe_video_pix_fmt(path: str) -> str | None:
    import subprocess
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=pix_fmt",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return None


def _sqr_video_has_audio(path: str) -> bool:
    import subprocess
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except Exception:
        return False


def _sqr_resolve_merge_encode_opts(
    video_paths: list,
    prompt: dict | None,
    vc_nid: str | None,
) -> dict:
    opts = _sqr_read_vhs_encode_opts(prompt, vc_nid)
    if video_paths:
        probed = _sqr_probe_video_pix_fmt(video_paths[0])
        if probed:
            opts["pix_fmt"] = probed
    return opts


def _sqr_x264_merge_args(pix_fmt: str, crf: int) -> list:
    args = ["-c:v", "libx264", "-preset", "fast", f"-crf", str(crf), "-pix_fmt", pix_fmt]
    if "10" in pix_fmt:
        args.extend(["-profile:v", "high10"])
    args.extend(["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"])
    return args


# ── 段间色彩匹配（羽化）─────────────────────────────────────────────
# 每段独立 VAE 解码会让相邻段交界出现一次性的全局色阶台阶（肉眼可见的"跳色"）。
# 这里在合并时测量每段开头相对上段结尾的色偏，给该段开头若干帧叠加一个
# 线性衰减到 0 的补偿（羽化），把"台阶"变成看不出的"缓坡"，段身完全不动。
def _sqr_color_match_config() -> dict:
    def _f(name, default):
        try:
            return float(str(os.environ.get(name, "")).strip() or default)
        except Exception:
            return default
    enabled = str(os.environ.get("SQR_COLOR_MATCH", "1")).strip().lower() \
        not in ("0", "false", "no", "off")
    return {
        "enabled": enabled,
        "feather": max(1, int(_f("SQR_COLOR_MATCH_FEATHER", 12))),
        "min_delta": _f("SQR_COLOR_MATCH_MIN", 1.0),
        "max_delta": _f("SQR_COLOR_MATCH_MAX", 25.0),
    }


def _sqr_first_last_mean(path):
    """(first_bgr, last_bgr) 平均色；失败返回 (None, None)。"""
    try:
        import cv2
    except Exception:
        return None, None
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None, None
    first = last = None
    try:
        ok, fr = cap.read()
        if ok and fr is not None:
            m = cv2.mean(fr); first = (m[0], m[1], m[2]); last = first
            n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if n > 1:
                cap.set(cv2.CAP_PROP_POS_FRAMES, n - 1)
                ok2, fr2 = cap.read()
                if ok2 and fr2 is not None:
                    m2 = cv2.mean(fr2); last = (m2[0], m2[1], m2[2])
                else:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0); cap.read()
                    while True:
                        ok3, fr3 = cap.read()
                        if not ok3:
                            break
                        m3 = cv2.mean(fr3); last = (m3[0], m3[1], m3[2])
    except Exception:
        pass
    finally:
        cap.release()
    return first, last


def _sqr_measure_boundary_offsets(video_paths, cfg):
    """每段开头需叠加的 BGR 偏移（对齐到上段结尾）。死区/上限内置 0。
    测量失败返回 None（调用方据此跳过色彩匹配）。"""
    n = len(video_paths)
    fl = [_sqr_first_last_mean(p) for p in video_paths]
    if any(x[0] is None or x[1] is None for x in fl):
        return None
    offsets = [(0.0, 0.0, 0.0)]
    for i in range(1, n):
        prev_last, cur_first = fl[i - 1][1], fl[i][0]
        off = tuple(prev_last[c] - cur_first[c] for c in range(3))
        mx = max(abs(c) for c in off)
        if mx < cfg["min_delta"] or mx > cfg["max_delta"]:
            off = (0.0, 0.0, 0.0)
        offsets.append(off)
    return offsets


def _sqr_geq_feather(offset_bgr, feather: int):
    """构造对单段开头羽化叠加色偏的 geq 滤镜串；偏移为 0 时返回 None。"""
    oB, oG, oR = offset_bgr
    if max(abs(oB), abs(oG), abs(oR)) <= 0:
        return None
    K = max(1, int(feather))

    def _e(src, o):
        return f"clip({src}+{o:.4f}*max(0\\,1-N/{K})\\,0\\,255)"

    return (f"geq=r='{_e('r(X\\,Y)', oR)}'"
            f":g='{_e('g(X\\,Y)', oG)}'"
            f":b='{_e('b(X\\,Y)', oB)}'")


def _sqr_build_unified_merge_cmd(
    video_paths: list,
    output_path: str,
    target_fps: float,
    pix_fmt: str,
    crf: int,
    offsets=None,
    feather: int = 12,
) -> list | None:
    """Normalize each segment to the same fps/format before concat (reduces boundary color pops).
    offsets[i] 非零时，对第 i 段开头做羽化色彩匹配（见 _sqr_geq_feather）。"""
    n = len(video_paths)
    if n < 2:
        return None
    fps_str = f"{target_fps:.6f}".rstrip("0").rstrip(".")
    has_audio = all(_sqr_video_has_audio(p) for p in video_paths)
    cmd = ["ffmpeg", "-y"]
    for p in video_paths:
        cmd.extend(["-i", p])
    parts = []
    v_labels = []
    for i in range(n):
        feat = _sqr_geq_feather(offsets[i], feather) if (offsets and i < len(offsets)) else None
        if feat:
            parts.append(f"[{i}:v]fps={fps_str},format=gbrp,{feat},format={pix_fmt},setpts=PTS-STARTPTS[v{i}]")
        else:
            parts.append(f"[{i}:v]fps={fps_str},format={pix_fmt},setpts=PTS-STARTPTS[v{i}]")
        v_labels.append(f"[v{i}]")
    parts.append(f"{''.join(v_labels)}concat=n={n}:v=1:a=0[vout]")
    if has_audio:
        a_labels = []
        for i in range(n):
            parts.append(
                f"[{i}:a]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0[a{i}]"
            )
            a_labels.append(f"[a{i}]")
        parts.append(f"{''.join(a_labels)}concat=n={n}:v=0:a=1[aout]")
    cmd.extend(["-filter_complex", ";".join(parts), "-map", "[vout]"])
    cmd.extend(_sqr_x264_merge_args(pix_fmt, crf))
    if has_audio:
        cmd.extend(["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"])
    else:
        cmd.append("-an")
    cmd.append(output_path)
    return cmd


def merge_videos(
    video_paths: list,
    output_path: str,
    target_fps: float = None,
    pix_fmt: str | None = None,
    crf: int | None = None,
) -> bool:
    import subprocess, tempfile
    if not video_paths:
        return False
    _pix = (pix_fmt or "yuv420p").strip() or "yuv420p"
    _crf = _sqr_parse_int(crf, 18)
    list_path = None
    try:
        if target_fps and target_fps > 0 and len(video_paths) >= 2:
            cfg = _sqr_color_match_config()
            offsets = None
            if cfg["enabled"]:
                try:
                    offsets = _sqr_measure_boundary_offsets(video_paths, cfg)
                    if offsets and any(max(abs(c) for c in o) > 0 for o in offsets):
                        nz = [(i, tuple(round(c, 2) for c in o))
                              for i, o in enumerate(offsets) if max(abs(c) for c in o) > 0]
                        print(f"[SQR] 段间色彩匹配(羽化{cfg['feather']}帧): {nz}")
                    elif offsets is None:
                        print("[SQR] 段间色彩匹配: 测量不可用(无cv2/读帧失败)，跳过")
                    else:
                        print("[SQR] 段间色彩匹配: 各接缝偏移在死区内，无需修正")
                except Exception as e:
                    print(f"[SQR] 段间色彩匹配测量异常，跳过: {e}")
                    offsets = None
            unified_cmd = _sqr_build_unified_merge_cmd(
                video_paths, output_path, target_fps, _pix, _crf,
                offsets=offsets, feather=cfg["feather"])
            if unified_cmd:
                result = subprocess.run(unified_cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    return True
                print(f"[SQR] 统一色彩合并失败，回退 concat: {result.stderr[-300:]}")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False, encoding="utf-8") as f:
            for p in video_paths:
                f.write("file " + repr(p) + "\n")
            list_path = f.name
        if target_fps and target_fps > 0:
            fps_str = f"{target_fps:.6f}".rstrip("0").rstrip(".")
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                   "-i", list_path,
                   "-vf", f"fps={fps_str},format={_pix}", "-vsync", "cfr"]
            cmd.extend(_sqr_x264_merge_args(_pix, _crf))
            cmd.extend(["-c:a", "aac", "-b:a", "192k", output_path])
        else:
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                   "-i", list_path, "-c", "copy", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        if target_fps and target_fps > 0:
            print(f"[SQR] CFR 合并失败，尝试无转码拼接: {result.stderr[-300:]}")
            fallback_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                            "-i", list_path, "-c", "copy", output_path]
            result = subprocess.run(fallback_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return True
        print(f"[SQR] ffmpeg 错误: {result.stderr[-300:]}")
        return False
    except FileNotFoundError:
        print("[SQR] ✗ 未找到 ffmpeg，请确认系统已安装 ffmpeg 并在 PATH 中")
        return False
    except Exception as e:
        print(f"[SQR] ✗ 合并异常: {e}")
        return False
    finally:
        if list_path:
            try:
                os.unlink(list_path)
            except Exception:
                pass


class SegmentQueueRunner:
    CATEGORY = "video/utils"
    FUNCTION = "run"
    OUTPUT_NODE = True
    RETURN_TYPES = ()
    RETURN_NAMES = ()

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "frame_rate": ("FLOAT", {"default": 16.0, "min": 1.0, "max": 120.0, "forceInput": True,
                    "tooltip": "Video frame rate. Connect this to the fps output of Load Video."}),
                "total_frames": ("INT", {"default": 0, "min": 0, "max": 99999, "forceInput": True,
                    "tooltip": "Total number of frames in the reference video. Connect this to the frame_count output of Load Video."}),
                "segment_count": ("INT", {"default": 2, "min": 1, "max": 100, "step": 1, "display": "slider",
                    "tooltip": "Number of evenly divided segments. The maximum can be adjusted in settings."}),
                "start_segment": ("INT", {"default": 1, "min": 1, "max": 100, "step": 1, "display": "slider",
                    "tooltip": "Segment number to start from. Set this to the actual start segment when resuming."}),
                "execute": ("BOOLEAN", {"default": False,
                    "tooltip": "Off = preview the segment plan only. On = start execution."}),
                "enable_resume": ("BOOLEAN", {"default": False,
                    "tooltip": "When enabled, use the selected video above as the transition source for the first segment."}),
                "reference_video_node_id": ("STRING", {"default": ""}),
                "output_node_id":          ("STRING", {"default": ""}),
                "animate_embeds_node_id":  ("STRING", {"default": ""}),
                "reference_images_node_id": ("STRING", {"default": ""}),
                "segment_reference_images": ("STRING", {"default": ""}),
                "resume_video_path":       ("STRING", {"default": ""}),
                "sqr_save_png":            ("STRING", {"default": "true"}),
                "sqr_frame_offset":        ("STRING", {"default": "-1"}),
                "sqr_pre_segments":        ("STRING", {"default": ""}),

            },
            "hidden": {
                "transition_skip_frames": ("INT", {"default": -1}),
                "prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID",

            },
        }

    def run(self,
            total_frames=None, frame_rate=None, segment_count=None, start_segment=None,
            execute=None, enable_resume=None,
            reference_video_node_id=None, output_node_id=None, animate_embeds_node_id=None, reference_images_node_id=None,
            segment_reference_images=None, resume_video_path=None,
            sqr_save_png="true",
            sqr_frame_offset="-1",
            sqr_pre_segments="",
            transition_skip_frames=-1,
            prompt=None, extra_pnginfo=None, unique_id=None, **legacy_kwargs):

        total_frames = _sqr_parse_int(
            total_frames if total_frames is not None else legacy_kwargs.get("总帧数", 0), 0)
        frame_rate = float(frame_rate if frame_rate is not None else legacy_kwargs.get("帧率", 0) or 0)
        segments = _sqr_parse_int(
            segment_count if segment_count is not None else legacy_kwargs.get("分段数", 2), 2)
        start_segment = _sqr_parse_int(
            start_segment if start_segment is not None else legacy_kwargs.get("从第几段开始", 1), 1)
        start_segment = max(1, start_segment)
        execute = bool(execute if execute is not None else legacy_kwargs.get("执行", False))
        enable_resume = bool(enable_resume if enable_resume is not None else legacy_kwargs.get("启用续跑", False))
        reference_video_node_id = str(reference_video_node_id if reference_video_node_id is not None else legacy_kwargs.get("参考视频节点ID", "") or "")
        output_node_id = str(output_node_id if output_node_id is not None else legacy_kwargs.get("输出节点ID", "") or "")
        animate_embeds_node_id = str(animate_embeds_node_id if animate_embeds_node_id is not None else legacy_kwargs.get("动作嵌入节点ID", "") or "")
        reference_images_node_id = str(reference_images_node_id if reference_images_node_id is not None else legacy_kwargs.get("参考图节点ID", "") or "")
        segment_reference_images = str(segment_reference_images if segment_reference_images is not None else legacy_kwargs.get("分段参考图", "") or "")
        resume_video_path = str(resume_video_path if resume_video_path is not None else legacy_kwargs.get("续跑视频路径", "") or "")
        transition_skip_frames = _sqr_parse_int(
            transition_skip_frames if transition_skip_frames is not None
            else legacy_kwargs.get("过渡跳过帧数", -1), -1)

        node_id            = reference_video_node_id.strip()
        combine_nid        = output_node_id.strip()
        ae_node_id         = animate_embeds_node_id.strip()
        resume_video_path  = resume_video_path.strip()
        resume_enabled     = bool(resume_video_path or enable_resume)
        skip_frames_manual = transition_skip_frames
        ri_node_id         = reference_images_node_id.strip()
        ref_imgs_str       = segment_reference_images.strip()

        _frame_offset_param = _sqr_parse_int(sqr_frame_offset, -1)
        if _frame_offset_param < 0 and prompt and unique_id:
            _self_inputs = (prompt or {}).get(str(unique_id), {}).get("inputs", {})
            _fo_val = _self_inputs.get("sqr_frame_offset", -1)
            _frame_offset_param = _sqr_parse_int(_fo_val, -1)
        _frame_offset = _frame_offset_param if _frame_offset_param >= 0 else 0

        _plan_frames = max(1, total_frames - _frame_offset) if _frame_offset > 0 else total_frames

        _preview_segments = segments
        start_from_segment = max(1, min(start_segment, _preview_segments))
        plan_text = build_plan_text(
            _plan_frames, _preview_segments, start_from_segment, node_id, frame_rate)

        def _do_interrupt():
            try:
                from comfy import model_management as _mm
                _mm.interrupt_current_processing()
                print("[SQR] ✓ 中断标志已设置（内部API）。")
                return
            except Exception:
                pass
            try:
                interrupt_current()
                print("[SQR] ✓ 中断标志已设置（HTTP）。")
            except Exception as _e:
                print(f"[SQR] ⚠ 中断设置失败: {_e}")

        if not execute:
            msg = "[预览模式]\n" + plan_text
            def _pi(): time.sleep(0.005); _do_interrupt()
            threading.Thread(target=_pi, daemon=True).start()
            _sqr_log(unique_id, msg)
            return {}

        if total_frames <= 0:
            _sqr_log(unique_id, "[SQR] ✗ 总帧数必须大于 0。")
            return {}
        if not node_id:
            _sqr_log(unique_id, "[SQR] ✗ 参考视频节点ID 不能为空。")
            return {}

        _sqr_full_prompt = (extra_pnginfo or {}).get("sqr_full_prompt")
        _effective_prompt = _sqr_full_prompt if _sqr_full_prompt else prompt
        _need_interrupt = (_sqr_full_prompt is None)
        _client_id = str((extra_pnginfo or {}).get("sqr_client_id") or "")
        _is_remote = bool((extra_pnginfo or {}).get("sqr_is_remote", False))

        if node_id not in (_effective_prompt or {}):
            _sqr_log(unique_id, f"[SQR] ✗ 找不到节点 ID「{node_id}」（完整工作流中）。")
            return {}

        print(f"[SQR] sqr_frame_offset: 参数={sqr_frame_offset}, 实际使用={_frame_offset}"
              f" | 工作流来源={'extra_pnginfo' if _sqr_full_prompt else 'prompt(回退)'}"
              f" | 分段模式=average")
        _effective_frames = max(1, total_frames - _frame_offset) if _frame_offset > 0 else total_frames

        seg_list = calc_segments(_effective_frames, segments)

        start_idx   = start_from_segment - 1
        segs_to_run = seg_list[start_idx:]
        base_prompt = copy.deepcopy(_effective_prompt)

        ae_nid = ae_node_id or find_animate_embeds_node(base_prompt) or ""
        vc_nid = find_video_combine_node(base_prompt, combine_nid) or ""
        _ae_class_type = base_prompt.get(ae_nid, {}).get("class_type", "") if ae_nid else ""
        _default_trans, _default_trim_front, _default_trim_back = _sqr_transition_constants(_ae_class_type)

        ref_images_list = [x.strip() for x in ref_imgs_str.split(",") if x.strip()]                           if ref_imgs_str else []
        if ref_images_list:
            ref_images_list = _sqr_prepare_checkpoint_ref_images(ref_images_list, unique_id=unique_id)

        manual_video_path = manual_video_frames = None
        manual_trans_dir = None
        if resume_enabled and resume_video_path:
            _resume_dir = _sqr_resolve_trans_dir(resume_video_path)
            _resume_dir_n = _sqr_count_dir_images(_resume_dir) if _resume_dir else 0
            if _resume_dir and _resume_dir_n > 0:
                manual_trans_dir = _resume_dir
                manual_video_frames = _resume_dir_n
                _sqr_log(unique_id, f"[SQR] ✓ 续跑无损衔接帧目录: {os.path.basename(_resume_dir)} ({_resume_dir_n}张PNG)")
            else:
                p = _sqr_resolve_media_path(resume_video_path)
                if p and os.path.isfile(p):
                    try:
                        src_p = p
                        p = _sqr_copy_into_input(p, unique_id=unique_id, prefix="sqr_resume")
                        if os.path.realpath(src_p) != os.path.realpath(p):
                            _sqr_log(unique_id, f"[SQR] 已复制续跑视频到 input/: {os.path.basename(p)}")
                        fname = os.path.basename(p)
                        import cv2
                        cap = cv2.VideoCapture(p)
                        try:
                            if not cap.isOpened():
                                _sqr_log(unique_id, f"[SQR] ✗ cv2 无法打开续跑视频: {fname}")
                            else:
                                frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                                if frames <= 0:
                                    _sqr_log(unique_id, f"[SQR] ✗ 续跑视频帧数异常: {fname} ({frames})")
                                else:
                                    manual_video_frames = frames
                                    manual_video_path = p
                                    _sqr_log(unique_id, f"[SQR] ✓ 续跑视频: {fname} ({manual_video_frames}帧)")
                        finally:
                            cap.release()
                    except Exception as e:
                        _sqr_log_cv2_issue(unique_id, "读取续跑视频失败", e)
                else:
                    _sqr_log(unique_id, f"[SQR] ⚠ 续跑视频不存在或无法解析: {resume_video_path}")

        width_src = height_src = None
        target_inputs = base_prompt.get(node_id, {}).get("inputs", {})
        if "custom_width" in target_inputs and isinstance(target_inputs["custom_width"], list):
            width_src = target_inputs["custom_width"]
        if "custom_height" in target_inputs and isinstance(target_inputs["custom_height"], list):
            height_src = target_inputs["custom_height"]

        def log(msg: str):
            _sqr_log(unique_id, f"[SQR] {msg}")

        audio_filename = find_audio_filename(base_prompt, node_id)
        if audio_filename:
            _sqr_log(unique_id, f"[SQR] 音频文件: {audio_filename}")
        else:
            _sqr_log(unique_id, f"[SQR] ⚠ 无法获取音频文件名")

        image_src_node = None
        image_src_note = ""
        if vc_nid and vc_nid in base_prompt:
            image_src_node, _img_src_nid, _img_src_ct, image_src_note = _sqr_resolve_image_src(
                base_prompt, vc_nid, ae_nid)
            if image_src_node:
                print(f"[SQR] 图像来源: {image_src_node} ({image_src_note})")
            else:
                print(f"[SQR] ⚠ 图像来源未解析: {image_src_note}")

        pre_segment_paths = [p.strip() for p in sqr_pre_segments.split(",")
                             if p.strip() and os.path.isfile(p.strip())] \
                            if sqr_pre_segments.strip() else []
        if pre_segment_paths:
            print(f"[SQR] 续跑前段素材: {len(pre_segment_paths)} 个文件")

        run_stamp = _sqr_now_stamp()

        def submit_all():
            last_video_path   = manual_video_path
            last_video_frames = manual_video_frames
            last_trans_dir    = manual_trans_dir
            segment_output_paths = []
            sqr_cut_cleanup = []
            sqr_cut_paths   = []
            _t0 = time.time()
            _total_frames_ran = sum(limit for _, limit in segs_to_run)
            _all_done = False

            log(f"{'═'*20} 运行时间码={run_stamp} {'═'*20}")
            log(f"AnimateEmbeds节点: [{ae_nid}]"
                f" ({_ae_class_type or 'unknown'})")
            if _ae_class_type == "WanAnimatePlus AnimateEmbeds":
                log(f"WAP分段规则: 读n+{_WAP_LOAD_EXTRA}帧，过渡{_default_trans}帧，裁尾{_default_trim_back}帧→保存n帧")
            log(f"输出节点: [{vc_nid}]")
            if ref_images_list:
                log(f"参考图列表: {ref_images_list}")
            if _frame_offset > 0:
                log(f"=== 重新设计续跑模式（帧偏移={_frame_offset}，跳过前{_frame_offset}帧参考视频）===")
            elif resume_enabled:
                log(f"=== 自动续跑模式 ===")
            else:
                log(f"=== 全新生成 ===")
            if resume_enabled:
                if manual_video_path:
                    log(f"✓ 续跑视频: {os.path.basename(manual_video_path)} ({manual_video_frames}帧)")
                else:
                    log(f"⚠ 续跑已启用但视频无效，首段无过渡")

            for i, (skip, limit) in enumerate(segs_to_run):
                seg_num        = start_idx + i + 1
                total_segs     = len(seg_list)
                use_transition = (last_video_path is not None) or (last_trans_dir is not None)
                wf             = copy.deepcopy(base_prompt)
                _seg_ae_type = wf.get(ae_nid, {}).get("class_type", _ae_class_type) if ae_nid else _ae_class_type
                TRANSITION_FRAMES, TRIM_FRONT, TRIM_BACK = _sqr_transition_constants(_seg_ae_type)
                audio_skip_frames = skip

                _actual_skip = skip + _frame_offset
                if _frame_offset > 0:
                    log(f"--- 第{seg_num}/{total_segs}段  实际skip={_actual_skip}（段内{skip}+偏移{_frame_offset}）limit={limit} ---")
                else:
                    log(f"--- 第{seg_num}/{total_segs}段  skip={_actual_skip}  limit={limit} ---")

                _gen_frames = limit
                _wap_widget_frames = limit
                if _seg_ae_type == "WanAnimatePlus AnimateEmbeds":
                    _wap_widget_frames = _sqr_wap_num_frames_for_widget(limit)
                    _gen_frames = _wap_widget_frames

                wf[node_id]["inputs"]["skip_first_frames"] = _actual_skip
                wf[node_id]["inputs"]["frame_load_cap"]    = _gen_frames

                if ae_nid and ae_nid in wf:
                    _prev_nf = _sqr_read_int_input(wf[ae_nid], "num_frames", 0)
                    if _seg_ae_type == "WanAnimatePlus AnimateEmbeds":
                        _sqr_set_int_input(
                            wf, ae_nid, "num_frames", _wap_widget_frames, _WAP_NUM_FRAMES_WIDGET_IDX)
                        log(f"  ✓ num_frames/load_cap → widget={_wap_widget_frames} cap={_gen_frames} (保存{limit}帧)"
                            + (f"，原{ _prev_nf}" if _prev_nf and _prev_nf != _wap_widget_frames else ""))
                    else:
                        _sqr_set_int_input(
                            wf, ae_nid, "num_frames", _gen_frames, _WAP_NUM_FRAMES_WIDGET_IDX)
                        log(f"  ✓ num_frames/load_cap → {_gen_frames} (保存{limit}帧)"
                            + (f"，原{ _prev_nf}" if _prev_nf and _prev_nf != _gen_frames else ""))

                _ref_force_rate = wf.get(node_id, {}).get("inputs", {}).get("force_rate", 0)
                if isinstance(_ref_force_rate, list):
                    _ref_force_rate = 0
                else:
                    try:
                        _ref_force_rate = float(_ref_force_rate or 0)
                    except (TypeError, ValueError):
                        _ref_force_rate = 0
                _tv_force_rate = _ref_force_rate if _ref_force_rate > 0 else frame_rate

                if vc_nid and vc_nid in wf and audio_filename:
                    _real_skip = skip + _frame_offset
                    if _seg_ae_type == "WanAnimatePlus AnimateEmbeds":
                        main_audio_frames = _real_skip
                        audio_skip_frames = _real_skip
                        if use_transition:
                            transition_note = (
                                f"WAP音频 skip={main_audio_frames}帧"
                                f"（参考视频跳过前{_real_skip}帧；过渡21帧来自上段成片）"
                            )
                        else:
                            transition_note = f"WAP音频 skip={_real_skip}帧"
                    elif use_transition:
                        audio_skip_frames    = max(0, _real_skip - TRIM_FRONT)
                        main_audio_frames    = max(0, _real_skip - TRANSITION_FRAMES)
                        transition_note      = (
                            f"主节点skip{_real_skip}-{TRANSITION_FRAMES}={main_audio_frames}帧, "
                            f"cut_vc skip{_real_skip}-{TRIM_FRONT}={audio_skip_frames}帧"
                        )
                    else:
                        audio_skip_frames    = _real_skip
                        main_audio_frames    = _real_skip
                        transition_note      = f"{_real_skip}帧"
                    audio_start_time  = main_audio_frames / frame_rate
                    _audio_duration = 0
                    if _seg_ae_type == "WanAnimatePlus AnimateEmbeds" and frame_rate > 0:
                        _audio_duration = limit / frame_rate
                    audio_tmp_id      = f"sqr_audio_{seg_num}"
                    wf[audio_tmp_id] = {
                        "class_type": "VHS_LoadAudioUpload",
                        "inputs": {
                            "audio":      audio_filename,
                            "start_time": audio_start_time,
                            "duration":   _audio_duration,
                        }
                    }
                    wf[vc_nid]["inputs"]["audio"] = [audio_tmp_id, 0]
                    log(f"  ✓ 主节点音频: start={audio_start_time:.3f}s ({transition_note})")
                elif vc_nid and vc_nid in wf:
                    wf[vc_nid]["inputs"]["audio"] = [node_id, 2]
                    log(f"  ⚠ 音频: 无法获取文件名，直接用LoadVideo音频(skip={skip}帧)")

                if ae_nid and ae_nid in wf:
                    if use_transition:
                        tv_tmp_id = f"sqr_tv_{seg_num}"
                        _has_mp4 = bool(last_video_path)
                        _use_png_trans = bool(last_trans_dir) and (
                            _sqr_lossless_transition_enabled() or not _has_mp4)
                        if _use_png_trans:
                            # 无损 PNG 衔接：PIL 直读 RGB，无 YUV round-trip → 零色偏
                            wf[tv_tmp_id] = {
                                "class_type": "VHS_LoadImagesPath",
                                "inputs": {
                                    "directory":         last_trans_dir,
                                    "image_load_cap":    TRANSITION_FRAMES,
                                    "skip_first_images": 0,
                                    "select_every_nth":  1,
                                },
                            }
                            wf[ae_nid]["inputs"]["transition_video"] = [tv_tmp_id, 0]
                            log(
                                f"  ✓ 过渡(无损PNG): {os.path.basename(last_trans_dir)} "
                                f"取{TRANSITION_FRAMES}帧（零色偏衔接）"
                            )
                        else:
                            if skip_frames_manual >= 0:
                                t_skip = skip_frames_manual
                            elif last_video_frames:
                                # Author spec: last 21 frames of previous segment export (must match seg N end).
                                t_skip = max(0, last_video_frames - TRANSITION_FRAMES)
                            else:
                                t_skip = 0
                            tv_inputs = {
                                "video":             os.path.basename(last_video_path),
                                "force_rate":        _tv_force_rate,
                                "custom_width":      0,
                                "custom_height":     0,
                                "frame_load_cap":    TRANSITION_FRAMES,
                                "skip_first_frames": t_skip,
                                "select_every_nth":  1,
                                "format":            "AnimateDiff",
                            }
                            if width_src:
                                tv_inputs["custom_width"]  = width_src
                            if height_src:
                                tv_inputs["custom_height"] = height_src
                            wf[tv_tmp_id] = {"class_type": "VHS_LoadVideo", "inputs": tv_inputs}
                            wf[ae_nid]["inputs"]["transition_video"] = [tv_tmp_id, 0]
                            log(
                                f"  ✓ 过渡视频: {os.path.basename(last_video_path)} "
                                f"skip={t_skip} limit={TRANSITION_FRAMES}（上段最后{TRANSITION_FRAMES}帧）"
                            )
                    else:
                        wf[ae_nid]["inputs"].pop("transition_video", None)
                        log(f"  首段无过渡")

                if ref_images_list and ri_node_id and ri_node_id in wf:
                    img_idx = _sqr_pick_ref_image_idx(
                        start_idx + i, seg_list, _effective_frames, len(ref_images_list))
                    img_entry = ref_images_list[img_idx]
                    if os.path.isabs(img_entry):
                        import shutil as _shutil
                        input_dir = folder_paths.get_input_directory()
                        src_real  = os.path.realpath(img_entry)
                        if os.path.realpath(os.path.dirname(src_real)) == os.path.realpath(input_dir):
                            img_name = os.path.basename(src_real)
                        else:
                            img_fname = _build_safe_input_copy_name(src_real, unique_id=unique_id, prefix="sqr_refrun")
                            img_dst   = os.path.join(input_dir, img_fname)
                            try:
                                _shutil.copy2(src_real, img_dst)
                            except Exception as e:
                                log(f"  ⚠ 参考图复制失败: {e}")
                            img_name = img_fname
                    else:
                        img_name = img_entry
                    wf[ri_node_id]["inputs"]["image"] = img_name
                    wv = wf[ri_node_id].get("widgets_values", [])
                    if wv: wv[0] = img_name
                    log(f"  ✓ 参考图[{img_idx+1}]: {img_name}")

                _sqr_wire_ref_images(wf, ae_nid, ri_node_id, ref_images_list, log)

                is_last_seg = (seg_num == total_segs)
                _ae_node = wf.get(ae_nid, {}) if ae_nid else {}
                _wap_frame_window = _sqr_read_int_input(
                    _ae_node, "frame_window_size", 77) if _seg_ae_type == "WanAnimatePlus AnimateEmbeds" else 77
                _wap_has_start_ref = isinstance(
                    _ae_node.get("inputs", {}).get("start_ref_image"), list)
                _wap_has_ref_images = _sqr_wap_ref_in_use(_ae_node, ref_images_list)
                if ref_images_list and not _sqr_has_ref_images(_ae_node):
                    log("  ⚠ 已配置分段参考图，AnimateEmbeds.ref_images 未连线；仍按有参考图裁切")
                trim_start, save_len, crop_note, middle_len = _sqr_crop_plan(
                    _seg_ae_type, limit, use_transition, is_last_seg, total_segs,
                    frame_window_size=_wap_frame_window,
                    has_start_ref=_wap_has_start_ref,
                    has_ref_images=_wap_has_ref_images,
                    ref_images_list=ref_images_list,
                    ae_node=_ae_node,
                )

                image_src = image_src_node
                if not image_src and vc_nid and vc_nid in wf:
                    image_src, _isn, _ict, _inote = _sqr_resolve_image_src(wf, vc_nid, ae_nid)
                    if image_src:
                        log(f"  ✓ 裁切源: [{_isn}] {_ict} ({_inote})")
                elif image_src and image_src_note:
                    log(f"  ✓ 裁切源: [{image_src[0]}] ({image_src_note})")

                def _sqr_add_crop_chain(seg_n, src_link, skip_px, out_n, tail_trim=0):
                    """Skip skip_px front frames, drop tail_trim from end, keep out_n frames."""
                    cur = src_link
                    tag = "a"
                    if skip_px > 0:
                        sel_id = f"sqr_sel_{seg_n}_{tag}"
                        wf[sel_id] = {
                            "class_type": "VHS_SelectImages",
                            "inputs": {
                                "image": cur,
                                "indexes": f"{skip_px}:",
                                "err_if_missing": False,
                                "err_if_empty": True,
                            },
                        }
                        cur = [sel_id, 0]
                        tag = "b"
                    if tail_trim > 0:
                        sel_id = f"sqr_sel_{seg_n}_{tag}"
                        wf[sel_id] = {
                            "class_type": "VHS_SelectImages",
                            "inputs": {
                                "image": cur,
                                "indexes": f"-{out_n + tail_trim}:-{tail_trim}",
                                "err_if_missing": False,
                                "err_if_empty": True,
                            },
                        }
                        return sel_id
                    if skip_px > 0:
                        return f"sqr_sel_{seg_n}_a"
                    sel_id = f"sqr_sel_{seg_n}_a"
                    wf[sel_id] = {
                        "class_type": "VHS_SelectImages",
                        "inputs": {
                            "image": cur,
                            "indexes": f"0:{out_n}",
                            "err_if_missing": False,
                            "err_if_empty": True,
                        },
                    }
                    return sel_id

                if not image_src:
                    log("  ⚠ 无裁切图像源，VideoCombine 将使用原连线（可能含参考图前缀）")
                    main_image_node = final_image_node = None
                else:
                    if _seg_ae_type == "WanAnimatePlus AnimateEmbeds":
                        _tail_trim = _WAP_TAIL_TRIM
                    elif use_transition and is_last_seg:
                        _tail_trim = 0
                    else:
                        _tail_trim = TRIM_BACK
                    crop_nid = _sqr_add_crop_chain(
                        seg_num, image_src, trim_start, save_len, tail_trim=_tail_trim)
                    main_image_node = final_image_node = crop_nid
                log(f"  裁切：{crop_note}")

                _cut_audio_skip = audio_skip_frames

                if vc_nid and vc_nid in wf:
                    if main_image_node:
                        wf[vc_nid]["inputs"]["images"] = [main_image_node, 0]

                    cut_vc_id = f"sqr_cut_vc_{seg_num}"
                    cut_inputs = copy.deepcopy(wf[vc_nid]["inputs"])
                    if final_image_node:
                        cut_inputs["images"] = [final_image_node, 0]
                    cut_inputs["save_output"]     = True
                    cut_inputs["save_metadata"]   = False
                    _main_prefix = wf[vc_nid]["inputs"].get("filename_prefix", "")
                    _slash = max(_main_prefix.rfind("/"), _main_prefix.rfind("\\"))
                    _subfolder_prefix = _main_prefix[:_slash+1] if _slash >= 0 else ""
                    _cut_file_prefix = f"sqr_cut_{run_stamp}_seg{seg_num}_"
                    cut_inputs["filename_prefix"] = f"{_subfolder_prefix}{_cut_file_prefix}"

                    if audio_filename:
                        cut_audio_id = f"sqr_cut_audio_{seg_num}"
                        wf[cut_audio_id] = {
                            "class_type": "VHS_LoadAudioUpload",
                            "inputs": {
                                "audio":      audio_filename,
                                "start_time": _cut_audio_skip / frame_rate,
                                "duration":   (limit / frame_rate) if (
                                    _seg_ae_type == "WanAnimatePlus AnimateEmbeds" and frame_rate > 0
                                ) else 0,
                            }
                        }
                        cut_inputs["audio"] = [cut_audio_id, 0]
                        log(f"  ✓ cut_vc音频: start={_cut_audio_skip/frame_rate:.3f}s (={_cut_audio_skip}帧)")

                    wf[cut_vc_id] = {"class_type": "VHS_VideoCombine", "inputs": cut_inputs}
                    _cut_search_dir = os.path.join(folder_paths.get_output_directory(),
                                                   _subfolder_prefix.rstrip("/\\")) \
                                      if _subfolder_prefix else folder_paths.get_output_directory()
                    sqr_cut_cleanup.append((_cut_search_dir, _cut_file_prefix))

                    # 无损 PNG 衔接源：存末 TRANSITION_FRAMES 帧（非末段），供下段零色偏衔接
                    if (_sqr_lossless_transition_enabled() and final_image_node
                            and not is_last_seg and TRANSITION_FRAMES > 0):
                        _tf_sel_id = f"sqr_tframes_sel_{seg_num}"
                        wf[_tf_sel_id] = {
                            "class_type": "VHS_SelectImages",
                            "inputs": {
                                "image":          [final_image_node, 0],
                                "indexes":        f"-{TRANSITION_FRAMES}:",
                                "err_if_missing": False,
                                "err_if_empty":   True,
                            },
                        }
                        _tf_save_id = f"sqr_tframes_save_{seg_num}"
                        wf[_tf_save_id] = {
                            "class_type": "SaveImage",
                            "inputs": {
                                "images":          [_tf_sel_id, 0],
                                "filename_prefix": f"{_sqr_trans_frames_subfolder(run_stamp, seg_num)}/f",
                            },
                        }
                        log(f"  ✓ 无损衔接帧导出: {_sqr_trans_frames_subfolder(run_stamp, seg_num)}/ (末{TRANSITION_FRAMES}帧PNG)")

                if unique_id and unique_id in wf:
                    del wf[unique_id]

                log(f"  → 提交中...")
                try:
                    pid = queue_prompt(wf, client_id=_client_id)
                    log(f"  prompt_id={pid[:8]}...")
                    ok  = wait_for_prompt(pid)
                    if ok:
                        log(f"✓ 第{seg_num}段完成")
                        if is_last_seg:
                            _all_done = True
                        if unique_id and not _is_remote:
                            _lv_inputs = base_prompt.get(node_id, {}).get("inputs", {})
                            _ref_video_params = {
                                "video":             _lv_inputs.get("video", ""),
                                "force_rate":        _lv_inputs.get("force_rate", 0),
                                "frame_load_cap":    _lv_inputs.get("frame_load_cap", 0),
                                "skip_first_frames": _lv_inputs.get("skip_first_frames", 0),
                                "select_every_nth":  _lv_inputs.get("select_every_nth", 1),
                            }
                            _next_seg_idx = seg_num
                            if _next_seg_idx < len(seg_list):
                                _frame_offset_for_resume = _frame_offset + seg_list[_next_seg_idx][0]
                            else:
                                _frame_offset_for_resume = _frame_offset + (skip + limit)
                            _trans_fname = f"sqr_trans_{run_stamp}_seg{seg_num}.mp4"
                            _trans_dirname = (_sqr_trans_frames_subfolder(run_stamp, seg_num)
                                              if (_sqr_lossless_transition_enabled() and not is_last_seg) else "")
                            write_checkpoint(unique_id, {
                                "unique_id":              unique_id,
                                "run_stamp":                 run_stamp,
                                "completed_seg":          seg_num,
                                "total_segs":             total_segs,
                                "next_seg":               seg_num + 1,
                                "transition_video":       _trans_fname,
                                "transition_dir":         _trans_dirname,
                                "ref_images":             ref_images_list,
                                "segments":               segments,
                                "ref_video":              _ref_video_params.get("video", ""),
                                "ref_video_params":       _ref_video_params,
                                "timestamp":              time.strftime("%Y-%m-%d %H:%M:%S"),
                                "base_frame_offset":      _frame_offset,
                                "frame_offset_for_resume": _frame_offset_for_resume,
                                "total_frames_used":      total_frames,
                                "frame_rate_used":        frame_rate,
                            })
                        _elapsed = time.time() - _t0
                        _frames_done = sum(lmt for _, lmt in segs_to_run[:i+1])
                        save_speed_record(_elapsed, _frames_done)

                        cut_vc_id_done = f"sqr_cut_vc_{seg_num}"
                        cut_vpath = cut_vframes = None
                        _cut_has_audio = bool(audio_filename) or (
                            vc_nid and base_prompt.get(vc_nid, {}).get("inputs", {}).get("audio")
                        )
                        if vc_nid:
                            cut_vpath, cut_vframes = get_output_video_info(
                                pid, cut_vc_id_done, logger=log, prefer_audio=_cut_has_audio)
                            if not cut_vpath and sqr_cut_cleanup:
                                _cut_search_dir, _cut_file_prefix = sqr_cut_cleanup[-1]
                                _alt, _alt_n = _sqr_find_video_by_prefix(
                                    _cut_search_dir, _cut_file_prefix, prefer_audio=_cut_has_audio)
                                if _alt:
                                    cut_vpath, cut_vframes = _alt, _alt_n
                            if not cut_vpath:
                                cut_vpath, cut_vframes = get_output_video_info(
                                    pid, vc_nid, logger=log, prefer_audio=_cut_has_audio)
                            if cut_vpath:
                                segment_output_paths.append(cut_vpath)
                                sqr_cut_paths.append(cut_vpath)
                                _audio_tag = "含音轨" if "-audio" in os.path.basename(cut_vpath) else "仅视频"
                                log(f"  ✓ 裁切输出({_audio_tag}): {os.path.basename(cut_vpath)}")
                            else:
                                log(f"  ⚠ 未找到裁切输出视频")

                        vpath, vframes = get_output_video_info(pid, vc_nid, logger=log) if vc_nid else (None, None)
                        if vpath and vframes and ae_nid and _seg_ae_type == "WanAnimatePlus AnimateEmbeds":
                            _exp_saved = _sqr_wap_expected_full_frames(limit, use_transition, False)
                            _check_frames = cut_vframes or vframes
                            if abs(_check_frames - _exp_saved) > 3:
                                log(
                                    f"  ⚠ 成片{_check_frames}帧，预期保存{_exp_saved}帧"
                                    f"(gen={_gen_frames})，请确认 num_frames 已生效"
                                )
                            if _cut_has_audio and cut_vpath and "-audio" not in os.path.basename(cut_vpath):
                                log(f"  ⚠ 裁切输出无音轨，合并结果可能无声（请检查 VHS 音频连线）")
                        if not vpath and not cut_vpath:
                            log(f"  ⚠ 完整视频获取失败，下段过渡将跳过")

                        # 无损 PNG 衔接目录定位（供下段零色偏衔接，优先于 mp4）
                        last_trans_dir = None
                        if _sqr_lossless_transition_enabled() and not is_last_seg:
                            _png_dir = _sqr_trans_frames_dir(run_stamp, seg_num)
                            _png_n = _sqr_count_dir_images(_png_dir)
                            if _png_n > 0:
                                last_trans_dir = _png_dir
                                log(f"  ✓ 无损衔接目录就绪: {os.path.basename(_png_dir)} ({_png_n}张PNG)")
                            else:
                                log(f"  ⚠ 未找到无损衔接PNG，下段回退mp4过渡")

                        _trans_src = cut_vpath or vpath
                        _trans_count = cut_vframes or vframes
                        if _trans_src:
                            import shutil
                            input_dir   = folder_paths.get_input_directory()
                            input_fname = f"sqr_trans_{run_stamp}_seg{seg_num}.mp4"
                            input_path  = os.path.join(input_dir, input_fname)
                            try:
                                shutil.copy2(_trans_src, input_path)
                                last_video_path   = input_path
                                last_video_frames = _trans_count
                                _src_label = "裁切成片" if cut_vpath else "完整输出"
                                log(f"  ✓ 已复制({_src_label}): {input_fname} ({_trans_count}帧，供下段过渡21帧)")
                            except Exception as e:
                                log(f"  ✗ 复制失败: {e}")
                                last_video_path = last_video_frames = None
                        else:
                            log(f"  ⚠ 未找到过渡源视频，下段过渡将跳过")
                            last_video_path = last_video_frames = None
                    else:
                        log(f"✗ 第{seg_num}段出错，终止。")
                        break
                except Exception as e:
                    log(f"✗ 提交失败：{e}")
                    break

            if pre_segment_paths:
                log(f"续跑合并：前段 {len(pre_segment_paths)} 个 + 本次 {len(segment_output_paths)} 个")
                segment_output_paths = pre_segment_paths + segment_output_paths

            if len(segment_output_paths) >= 2:
                log(f"开始合并 {len(segment_output_paths)} 段视频...")
                output_dir   = folder_paths.get_output_directory()
                if vc_nid and base_prompt and vc_nid in base_prompt:
                    _mp = base_prompt[vc_nid]["inputs"].get("filename_prefix", "")
                    _sl = max(_mp.rfind("/"), _mp.rfind("\\"))
                    _sub = _mp[:_sl+1] if _sl >= 0 else ""
                    if _sub:
                        os.makedirs(os.path.join(output_dir, _sub.rstrip("/\\")), exist_ok=True)
                else:
                    _sub = ""
                merged_fname = f"sqr_merged_{run_stamp}.mp4"
                merged_path  = _sqr_unique_filepath(os.path.join(output_dir, _sub + merged_fname))
                merged_fname = os.path.basename(merged_path)
                _merge_fps = frame_rate if frame_rate and frame_rate > 0 else None
                _merge_enc = _sqr_resolve_merge_encode_opts(
                    segment_output_paths, base_prompt, vc_nid)
                if _merge_fps:
                    log(
                        f"合并方式: CFR {float(_merge_fps):.3f}fps + "
                        f"{_merge_enc['pix_fmt']} crf={_merge_enc['crf']}，"
                        f"各段先统一色彩格式再拼接"
                    )
                if merge_videos(
                    segment_output_paths,
                    merged_path,
                    target_fps=_merge_fps,
                    pix_fmt=_merge_enc.get("pix_fmt"),
                    crf=_merge_enc.get("crf"),
                ):
                    log(f"✓ 合并完成: {_sub + merged_fname}")
                else:
                    log(f"✗ 合并失败，请手动拼接各段视频")
            elif len(segment_output_paths) == 1:
                log(f"只有1段，无需合并")

            for (_clean_dir, _clean_prefix) in sqr_cut_cleanup:
                try:
                    if not os.path.isdir(_clean_dir):
                        continue
                    for _f in os.listdir(_clean_dir):
                        if not _f.startswith(_clean_prefix):
                            continue
                        _fpath = os.path.join(_clean_dir, _f)
                        if _f.endswith(".mp4") and "-audio" in _f:
                            continue
                        if _f.endswith(".mp4") or _f.endswith(".png"):
                            try:
                                os.remove(_fpath)
                                print(f"[SQR] 已清理临时文件: {_f}")
                            except Exception:
                                pass
                except Exception:
                    pass

            _sqr_save_png = (str(sqr_save_png).lower() != "false")
            _should_clean_main_png = not _sqr_save_png
            print(f"[SQR] Save png 设置: {sqr_save_png} → {'保留' if _sqr_save_png else '清理'}主节点 png")

            if _should_clean_main_png and vc_nid and base_prompt and vc_nid in base_prompt:
                try:
                    _main_prefix = base_prompt[vc_nid]["inputs"].get("filename_prefix", "")
                    _output_root = folder_paths.get_output_directory()
                    _sl = max(_main_prefix.rfind("/"), _main_prefix.rfind("\\"))
                    _sub = _main_prefix[:_sl+1] if _sl >= 0 else ""
                    _fname_prefix = _main_prefix[_sl+1:] if _sl >= 0 else _main_prefix
                    _search_dir = os.path.join(_output_root, _sub.rstrip("/\\")) if _sub else _output_root
                    if os.path.isdir(_search_dir) and _fname_prefix:
                        for _f in os.listdir(_search_dir):
                            if _f.startswith(_fname_prefix) and _f.endswith(".png"):
                                try:
                                    os.remove(os.path.join(_search_dir, _f))
                                    print(f"[SQR] 已清理主节点元数据图: {_f}")
                                except Exception:
                                    pass
                except Exception:
                    pass

            if unique_id:
                if _all_done:
                    clear_checkpoint(unique_id)
                    _sqr_cleanup_ref_images(ref_images_list, unique_id=unique_id)
                    print("[SQR] checkpoint 已清除（全部完成）")
                else:
                    print("[SQR] 任务中断，checkpoint 保留供续跑检测")

            log("═══ 全部完成 ═══")

        if unique_id:
            _old_ckpt = read_checkpoint(unique_id)
            _old_refs = _old_ckpt.get("ref_images", []) if isinstance(_old_ckpt, dict) else []
            clear_checkpoint(unique_id)
            _sqr_cleanup_ref_images(_old_refs, unique_id=unique_id, keep_paths=ref_images_list)

        if _frame_offset > 0:
            _mode_header = f"=== 重新设计续跑模式（帧偏移={_frame_offset}，跳过前{_frame_offset}帧）==="
        elif resume_enabled:
            _mode_header = "=== 自动续跑模式 ==="
        else:
            _mode_header = "=== 全新生成 ==="
        exec_msg = _mode_header + "\n" + plan_text

        t = threading.Thread(target=submit_all, daemon=True)
        t.start()
        if _need_interrupt:
            def _ei(): time.sleep(0.005); _do_interrupt()
            threading.Thread(target=_ei, daemon=True).start()
        _sqr_log(unique_id, exec_msg)
        return {}


NODE_CLASS_MAPPINGS        = {"SegmentQueueRunner": SegmentQueueRunner}
NODE_DISPLAY_NAME_MAPPINGS = {"SegmentQueueRunner": "Segment Queue Runner 🎬"}


# ── 后端 API ─────────────────────────────────────────────────────
@server.PromptServer.instance.routes.get("/sqr/logs")
async def sqr_get_logs(request):
    uid = request.rel_url.query.get("uid", "")
    return web.json_response({"logs": list(_sqr_log_buf.get(str(uid), []))})

@server.PromptServer.instance.routes.post("/sqr/logs/clear")
async def sqr_clear_logs(request):
    _sqr_log_clear(request.rel_url.query.get("uid", ""))
    return web.json_response({"ok": True})

@server.PromptServer.instance.routes.get("/sqr/checkpoint")
async def sqr_get_checkpoint(request):
    uid = request.rel_url.query.get("uid", "")
    if not uid:
        return web.json_response({"checkpoint": None})
    ckpt = read_checkpoint(uid)
    if ckpt:
        input_dir = folder_paths.get_input_directory()
        tv = ckpt.get("transition_video", "")
        tv_path = os.path.join(input_dir, tv) if tv else ""
        ckpt["transition_exists"] = os.path.isfile(tv_path)
        if ckpt["transition_exists"] and tv_path:
            tv_mtime   = os.path.getmtime(tv_path)
            ckpt_mtime = os.path.getmtime(get_checkpoint_path(uid))
            if tv_mtime > ckpt_mtime + 60:
                ckpt["transition_exists"] = False
        # 无损 PNG 衔接目录校验（优先用于续跑，零色偏）
        td = ckpt.get("transition_dir", "")
        td_path = os.path.join(folder_paths.get_output_directory(), td) if td else ""
        td_n = _sqr_count_dir_images(td_path) if td_path else 0
        ckpt["transition_dir_exists"] = td_n > 0
        if td_n > 0:
            ckpt["transition_dir_path"] = td_path
            ckpt["transition_exists"]   = True
        import urllib.parse as _up
        cur_params_str = request.rel_url.query.get("ref_params", "")
        ckpt_params    = ckpt.get("ref_video_params", {})
        if not ckpt_params and ckpt.get("ref_video"):
            ckpt_params = {"video": ckpt.get("ref_video")}
        if cur_params_str and ckpt_params:
            try:
                import json as _json
                cur_params = _json.loads(_up.unquote(cur_params_str))
                mismatches = []
                for key in ("video", "force_rate", "frame_load_cap", "skip_first_frames", "select_every_nth"):
                    cv = cur_params.get(key, None)
                    kv = ckpt_params.get(key, None)
                    if key == "video":
                        if str(cv or "") != str(kv or ""):
                            mismatches.append(key)
                    else:
                        try:
                            if float(cv or 0) != float(kv or 0):
                                mismatches.append(key)
                        except (TypeError, ValueError):
                            if str(cv) != str(kv):
                                mismatches.append(key)
                ckpt["ref_video_match"]    = len(mismatches) == 0
                ckpt["ref_video_mismatches"] = mismatches
            except Exception:
                ckpt["ref_video_match"] = True
        else:
            ckpt["ref_video_match"]    = True
            ckpt["ref_video_mismatches"] = []
    return web.json_response({"checkpoint": ckpt})


def _sqr_safe_upload_name(input_dir: str, original: str, default_ext: str) -> str:
    """生成不冲突的安全文件名（保留原扩展名，去除路径分隔符）。"""
    base = os.path.basename(str(original or "")).strip()
    if not base:
        base = f"upload{default_ext}"
    # 去掉危险字符
    base = base.replace("\\", "_").replace("/", "_")
    name, ext = os.path.splitext(base)
    if not ext:
        ext = default_ext
    # 文件名前缀，便于识别 + 避免与已有文件冲突
    safe = f"sqr_up_{name}{ext}"
    dst = os.path.join(input_dir, safe)
    if not os.path.exists(dst):
        return safe
    # 加时间戳兜底
    stamp = _sqr_now_stamp()
    safe = f"sqr_up_{name}_{stamp}{ext}"
    return safe


@server.PromptServer.instance.routes.post("/sqr/upload_images")
async def sqr_upload_images(request):
    """接收浏览器多文件上传，保存到 ComfyUI input/ 目录。"""
    saved = []
    try:
        input_dir = folder_paths.get_input_directory()
        os.makedirs(input_dir, exist_ok=True)
        reader = await request.multipart()
        async for part in reader:
            if part.name not in ("files[]", "files", "file"):
                continue
            filename = part.filename or ""
            if not filename:
                continue
            safe = _sqr_safe_upload_name(input_dir, filename, ".png")
            dst = os.path.join(input_dir, safe)
            try:
                with open(dst, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(1024 * 64)
                        if not chunk:
                            break
                        f.write(chunk)
                saved.append(safe)
            except Exception as e:
                print(f"[SQR] upload_images 写入失败 {filename}: {e}")
        return web.json_response({"saved": saved})
    except Exception as e:
        print(f"[SQR] upload_images 出错: {_sqr_format_exc(e)}")
        return web.json_response({"saved": saved, "error": str(e)})


@server.PromptServer.instance.routes.post("/sqr/upload_video")
async def sqr_upload_video(request):
    """接收浏览器单文件视频上传，保存到 ComfyUI input/ 目录。"""
    try:
        input_dir = folder_paths.get_input_directory()
        os.makedirs(input_dir, exist_ok=True)
        reader = await request.multipart()
        async for part in reader:
            if part.name not in ("file", "files[]", "files"):
                continue
            filename = part.filename or ""
            if not filename:
                continue
            safe = _sqr_safe_upload_name(input_dir, filename, ".mp4")
            dst = os.path.join(input_dir, safe)
            try:
                with open(dst, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                return web.json_response({"saved": safe})
            except Exception as e:
                print(f"[SQR] upload_video 写入失败 {filename}: {e}")
                return web.json_response({"saved": "", "error": str(e)})
        return web.json_response({"saved": "", "error": "未收到文件"})
    except Exception as e:
        print(f"[SQR] upload_video 出错: {_sqr_format_exc(e)}")
        return web.json_response({"saved": "", "error": str(e)})


@server.PromptServer.instance.routes.get("/sqr/list_images")
async def sqr_list_images(request):
    import re
    img_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    def nat_key(s):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]
    try:
        files = sorted([f for f in os.listdir(folder_paths.get_input_directory())
                        if os.path.splitext(f)[1].lower() in img_exts], key=nat_key)
    except Exception:
        files = []
    return web.json_response({"images": files})


@server.PromptServer.instance.routes.get("/sqr/list_videos")
async def sqr_list_videos(request):
    import re
    vid_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
    def sort_key(fname):
        m = re.match(r"sqr_trans_[0-9_]+_seg(\d+)\.mp4$", fname, re.IGNORECASE) or re.match(r"sqr_trans_[a-f0-9]+_seg(\d+)\.mp4$", fname, re.IGNORECASE)
        if m:
            return (0, int(m.group(1)), fname)
        m = re.match(r"segment_transition_seg(\d+)\.mp4$", fname, re.IGNORECASE)
        if m:
            return (0, int(m.group(1)), fname)
        parts = re.split(r"(\d+)", fname)
        return (1, 0, tuple(int(p) if p.isdigit() else p.lower() for p in parts))
    try:
        files = sorted(
            [f for f in os.listdir(folder_paths.get_input_directory())
             if os.path.splitext(f)[1].lower() in vid_exts],
            key=sort_key
        )
    except Exception:
        files = []
    return web.json_response({"videos": files})


@server.PromptServer.instance.routes.get("/sqr/video_thumb")
async def sqr_video_thumb(request):
    fpath = request.rel_url.query.get("file", "").strip()
    if not fpath:
        return web.Response(status=400)

    raw_path = fpath
    fpath = _sqr_resolve_media_path(fpath)
    if not fpath or not os.path.isfile(fpath):
        print(f"[SQR] video_thumb: 文件不存在或无法解析: {raw_path}")
        return web.Response(status=404)

    try:
        import cv2
        cap = cv2.VideoCapture(fpath)
        try:
            if not cap.isOpened():
                print(f"[SQR] video_thumb: cv2 无法打开视频: {fpath}")
                return web.Response(status=404)

            ok, frame = cap.read()
            if not ok or frame is None:
                print(f"[SQR] video_thumb: 读取首帧失败: {fpath}")
                return web.Response(status=404)
        finally:
            cap.release()

        h, w = frame.shape[:2]
        new_w = 160
        new_h = int(h * new_w / w)
        frame = cv2.resize(frame, (new_w, new_h))
        ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok2:
            print(f"[SQR] video_thumb: JPEG 编码失败: {fpath}")
            return web.Response(status=500)

        return web.Response(body=buf.tobytes(), content_type="image/jpeg")

    except ModuleNotFoundError as e:
        if getattr(e, "name", "") == "cv2":
            print("[SQR] video_thumb失败: 未安装 cv2 / opencv-python。请安装 requirements.txt 中的依赖后重启 ComfyUI。")
        else:
            print(f"[SQR] video_thumb失败: {_sqr_format_exc(e)}")
        return web.Response(status=500)
    except Exception as e:
        print(f"[SQR] video_thumb失败: {_sqr_format_exc(e)}")
        return web.Response(status=500)


@server.PromptServer.instance.routes.get("/sqr/browse_videos")
async def sqr_browse_videos(request):
    import re
    vid_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
    def nat_key(s):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]
    def sort_key(fname):
        m = re.match(r"sqr_trans_[0-9_]+_seg(\d+)\.mp4$", fname, re.IGNORECASE) or re.match(r"sqr_trans_[a-f0-9]+_seg(\d+)\.mp4$", fname, re.IGNORECASE)
        if m:
            return (0, int(m.group(1)), fname)
        m = re.match(r"segment_transition_seg(\d+)\.mp4$", fname, re.IGNORECASE)
        if m:
            return (0, int(m.group(1)), fname)
        parts = re.split(r"(\d+)", fname)
        return (1, 0, tuple(int(p) if p.isdigit() else p.lower() for p in parts))
    req_path = request.rel_url.query.get("path", "").strip()
    import platform, string as _str
    if req_path == "__drives__":
        drives = []
        if platform.system() == "Windows":
            for d in _str.ascii_uppercase:
                dp = d + ":\\"
                if os.path.exists(dp):
                    drives.append({"label": dp, "path": dp, "is_drive": True})
        else:
            drives.append({"label": "/", "path": "/", "is_drive": True})
        return web.json_response({"type": "roots", "roots": drives})
    if not req_path:
        starts = []
        for label, p in [("ComfyUI input", folder_paths.get_input_directory()),
                         ("ComfyUI output", folder_paths.get_output_directory())]:
            if os.path.isdir(p):
                starts.append({"label": label, "path": p})
        starts.append({"label": "此电脑", "path": "__drives__", "is_virtual": True})
        home = os.path.expanduser("~")
        for sub in ["Desktop", "桌面", "Videos", "视频", "Downloads", "下载"]:
            p = os.path.join(home, sub)
            if os.path.isdir(p):
                starts.append({"label": sub, "path": p})
        return web.json_response({"type": "roots", "roots": starts})
    req_path = os.path.realpath(req_path)
    if not os.path.isdir(req_path):
        return web.json_response({"error": "路径不存在"}, status=400)
    try:
        entries = os.listdir(req_path)
    except PermissionError:
        return web.json_response({"error": "无权限访问"}, status=403)
    folders = sorted([e for e in entries
                      if os.path.isdir(os.path.join(req_path, e))
                      and not e.startswith(".")], key=nat_key)
    videos  = sorted([e for e in entries
                      if os.path.splitext(e)[1].lower() in vid_exts], key=sort_key)
    parent  = os.path.dirname(req_path) if req_path != os.path.dirname(req_path) else None
    return web.json_response({
        "type":    "dir",
        "path":    req_path,
        "parent":  parent,
        "folders": folders,
        "videos":  videos,
    })


@server.PromptServer.instance.routes.get("/sqr/image_thumb")
async def sqr_image_thumb(request):
    fname = request.rel_url.query.get("file", "")
    if not fname:
        return web.Response(status=400)
    path = _sqr_resolve_media_path(fname)
    if not path or not os.path.isfile(path):
        return web.Response(status=404)
    return web.FileResponse(path, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })




