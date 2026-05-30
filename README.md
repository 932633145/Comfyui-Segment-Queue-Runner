# ComfyUI Segment Queue Runner

# [简体中文](README_CN.md) 

An automated long-video generation node for ComfyUI Wan Animate / KJ Context, supporting segmented generation, seamless transitions, auto scene switching, breakpoint resuming, auto merging, and audio sync.

## ✨ Key Features
- Segmented Generation: Automatically split long videos to avoid out-of-memory errors
- Seamless Transitions: Use last frame of previous segment for smooth continuity
- Auto Scene Switch: Support multi-reference images for style/character changes
- Breakpoint Resume: Continue from any segment after interruption
- Auto Merge: Automatically combine clips into a complete video
- Audio Sync: Auto-extract and align audio from source video
- Preview Mode: Check segment plan before rendering

## 📦 Installation
cd ComfyUI/custom_nodes

git clone https://github.com/FX-FeiHou/Comfyui-Segment-Queue-Runner.git

## 📢 Changelog

### [v2.7] - 2026-05-30
**New: Merge-Stage "Segment Color Match (Feathered)" — suppress residual boundary color shift**
- **What it solves**: After v2.6's lossless PNG transition removed the main cause (transition-frame color round-trip), some workflows still showed a slight one-off "color pop" at segment joins (a single step at the start of each segment).
- **Root cause**: Each segment is independently diffused + VAE-decoded, so adjacent segments have a tiny global level/tint drift. This is model-level and cannot be eliminated even with consistent transition guidance; it shows up as a step-like jump at each segment's first frame, which is the only part the eye catches.
- **Fix**: At final merge (`merge_videos`), cv2 measures each segment's start offset relative to the previous segment's end, then adds a **linearly decaying-to-zero** color compensation over the **first ~12 frames** (feather). The step becomes an invisible ramp; the **segment body is untouched and corrections do not accumulate across segments**; audio is preserved.
- **Effect**: Measured boundary jump drops from the largest in the clip to around the median (effectively invisible). Note this is **post-merge cosmetic smoothing** — it does not change generation.
- **Safety**: Offsets below a deadzone are skipped (don't touch already-good seams); offsets above a cap are skipped (likely a scene cut); falls back to old behavior if measurement fails or cv2 is missing.
- **Toggles (env vars)**:
  - `SQR_COLOR_MATCH`: default `1` (on); set `0`/`off` to disable and use the old merge path.
  - `SQR_COLOR_MATCH_FEATHER`: feather length in frames, default `12` (raise to 15–20 for larger jumps).
  - `SQR_COLOR_MATCH_MIN`: deadzone, default `1.0`; seams with smaller offset are left alone.
  - `SQR_COLOR_MATCH_MAX`: cap, default `25.0`; offsets above this are treated as scene cuts and skipped.
- **Note**: This is a Python backend change — **fully restart ComfyUI** to take effect (Ctrl+F5 is not enough).

### [v2.6] - 2026-05-30
**Core Fix: Segment-Boundary Color Shift (Lossless PNG Transition)**
- **Fixed per-segment color shift**: The color/temperature jump at the start of every non-first segment (the segment opens off-color, then recovers) is resolved.
- **Trigger**: Start of segment 2 and onward during continuous segmented generation; the first segment of a resume behaved the same way.
- **Root cause**: The transition (last 21 frames of the previous segment) was reloaded from the lossy `sqr_cut_*.mp4` (`yuv420p`). The `RGB → YUV420 (lossy) → RGB` round-trip — possibly compounded by a limited/full color-range mismatch — shifted the transition frames' colors. The model used those off-color frames as hard conditioning, so each segment inherited the shift at its start and drifted back to normal afterwards.
- **Fix**: The transition source now uses **lossless PNG**. Each segment exports its last 21 frames via `SaveImage` (PNG, to `output/sqr_tframes_<time>_segN/`), and the next segment reads them with `VHS_LoadImagesPath` (PIL reads RGB directly, no YUV round-trip → zero color drift). The resume/checkpoint chain (checkpoint field, frontend resume dialog, resume loader) was updated to use the PNG directory too.
- **Toggle**: Set the env var `SQR_LOSSLESS_TRANSITION=0` to revert to the old mp4-based transition.
- **Note**: `sqr_tframes_*` PNG folders are kept to support resume; clean them manually like `sqr_checkpoint_*` if needed.

### [v2.5] - 2026-05-28
**Core Fix: WAP Segment-Boundary Frame Loss**
- **Fixed segment desync**: When using `WanAnimatePlus AnimateEmbeds`, the bug where each segment lost ~20 frames of motion at its start has been resolved. Single-segment clips played in sequence in a video editor no longer show motion jumps at the joins.
- **Trigger condition**: Occurred when single-segment generation length exceeded `frame_window_size` (default 77), e.g. the common `limit=117`, `widget=125` setup.
- **Symptom**: Log lines like `⚠ output=97 frames, expected=117 (gen=125)` — the gap roughly matches the transition length (21).
- **Root cause**: Older code incorrectly assumed WAP Decode keeps the transition frames in looping mode, so SQR added an extra 21-frame skip. In reality, current WAP Decode strips the transition in both modes, causing a double-skip.
- **Fix**: Switched to a unified "take last `limit + tail` frames, then drop trailing `tail`" negative-index slicing pattern. This is robust whether Decode strips the transition or not — no more guessing.
- **Upgrade tip**: After updating, delete any leftover `sqr_checkpoint_*.json` in the plugin folder, or disable resume in the workflow and rerun.

### [v2.4] - 2026-04-06
**Core Update: Adaptive Enhancement & UI/UX Optimization**
- **ComfyUI Port Auto-Recognition**: Automatically adapt to local usage and remote calls (RH adaptation pending KJ's wrapper node merge)
- **UI Style Unification**: Modified and unified the style of partial button UI elements
- **Execution Mode Highlight**: Added edge highlight distinction for execution modes, with toggle switch in settings
- **Slider UI for Segmentation**: Replaced segment count/start segment input with draggable sliders, optimized maximum segment count settings for better usability
- **Native Popup Optimization**: Removed redundant built-in selectors, only retained Windows (local) or browser (remote) native popups for selecting images/videos
- **Reference Image Management**: 
  - Drag to sort selected reference images (hold left click)
  - Remove images (right click)
  - Duplicate images (left click) to reuse the same image multiple times (no need to replace images for unchanged scenes/styles)
- **File Naming Optimization**: Replaced random run identifiers in `sqr_cut_*`/`sqr_trans_*`/`sqr_merged_*` with sortable timestamps (time code format), maintaining anti-overwrite capability while improving file identification; breakpoint resume logic adapted accordingly
- **Dependency & Log Enhancement**: Added cv2 missing error logging, specified `opencv-python>=4.8` dependency

### [v2.0] - 2026-04-03
**Core Update: Multi-Task Parallel Queue Support**
- **New Task Queue**: Support for simultaneous submission of multiple generation tasks.
- **Random Interleaved Sampling**: Implemented random interleaved sampling logic between different tasks.
- **Dynamic Priority Merging**: "First-finished, first-merged" strategy to optimize workflow.

**Bug Fixes:**
- **Fixed Preview Error**: Resolved the issue where previews in the image selection box displayed incorrectly.
- **Fixed Segment Misalignment**: Corrected the potential misalignment between segmented samples during multi-task parallel processing.
- **Fixed Video Overwriting**: Resolved a critical bug where final video merges could be overwritten during multi-task execution.

## 🚀 Quick Start
1. Connect frame_count and fps from Load Video to this node
2. Set segment count, turn off Run to preview the plan first
3. Bind nodes via buttons:
   - Source Video Node (Load Video)
   - Output Node (VHS_VideoCombine)
   - Motion Embedding Node (WanVideoAnimateEmbeds)
4. Turn on Run to start automatic generation

## 🛠 Modes
- Preview: Show segment plan only, no rendering
- New Generation: Render from segment 1 and auto-merge
- Resume: Continue from interrupted video seamlessly

## 📌 FAQ
- Node ID empty: Bind required nodes using the on-node buttons
- Resume after interruption: Set start segment → select last video → enable resume → run
- Output path: output/sqr_merged_xxx.mp4
- ffmpeg missing: Install ffmpeg and add to system PATH
- Segment-boundary motion jump (fixed in v2.5): If clips played in sequence in a video editor show motion jumps at the joins, upgrade to v2.5+, delete any old `sqr_checkpoint_*.json`, then rerun. Validate by checking the log: `output=N frames, expected=N` should match, with no `⚠ output=xx, expected=xx` warnings.
- Residual boundary color pop (v2.7): If, after v2.6, segment joins still show a slight "pop at the start of each segment," that is the model-level global level drift from independently VAE-decoded segments. v2.7 adds merge-stage "Segment Color Match (Feathered)" that aligns the first ~12 frames of each segment with a decaying correction (on by default; `SQR_COLOR_MATCH=0` to disable). Restart ComfyUI to apply.

## 👥 Authors
FX-FeiHou & XueZi & wuwukaka

## 📄 License
MIT License
