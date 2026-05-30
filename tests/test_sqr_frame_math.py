"""
SQR 帧/过渡参数本地测试（无需启动 ComfyUI）。

运行:
  python tests/test_sqr_frame_math.py
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Mock ComfyUI-only imports so segment_queue_node can load outside ComfyUI.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

sys.modules.setdefault("server", MagicMock())
sys.modules.setdefault("folder_paths", MagicMock())
_aiohttp = MagicMock()
sys.modules.setdefault("aiohttp", _aiohttp)
sys.modules.setdefault("aiohttp.web", _aiohttp.web)

import segment_queue_node as sqr  # noqa: E402


def crop_output_frames(
    limit: int,
    *,
    use_transition: bool,
    is_last_seg: bool,
    transition_frames: int,
    trim_front: int,
    trim_back: int,
) -> int:
    """Mirror ImageFromBatch trim logic in submit_all()."""
    total_raw = limit + (transition_frames if use_transition else 0)
    if not use_transition:
        return total_raw - trim_back
    if is_last_seg:
        return total_raw - trim_front
    after_front = total_raw - trim_front
    return after_front - trim_back


class TestTransitionConstants(unittest.TestCase):
    def test_wan_animate_plus(self):
        self.assertEqual(
            sqr._sqr_transition_constants("WanAnimatePlus AnimateEmbeds"),
            (21, 21, 3),
        )

    def test_original_wan_video(self):
        self.assertEqual(
            sqr._sqr_transition_constants("WanVideoAnimateEmbeds"),
            (32, 16, 16),
        )
        self.assertEqual(sqr._sqr_transition_constants(None), (32, 16, 16))

    def test_wap_gen_frames(self):
        self.assertEqual(sqr._sqr_wap_gen_frames(69), 77)
        self.assertEqual(sqr._sqr_wap_gen_frames(200), 209)

    def test_wap_num_frames_widget(self):
        self.assertEqual(sqr._sqr_wap_num_frames_for_widget(69), 77)
        self.assertEqual(sqr._sqr_wap_num_frames_for_widget(65), 73)

    def test_wap_decode_strips_when_not_looping(self):
        self.assertTrue(sqr._sqr_wap_decode_strips_transition(69, True, 77, False))

    def test_wap_decode_strips_when_looping(self):
        self.assertTrue(sqr._sqr_wap_decode_strips_transition(69, True, 60, False))
        self.assertTrue(sqr._sqr_wap_decode_strips_transition(69, True, 77, True))

    def test_original_trim_sums_to_transition(self):
        for class_type, (trans, front, back) in (
            ("WanVideoAnimateEmbeds", (32, 16, 16)),
        ):
            with self.subTest(class_type=class_type):
                self.assertEqual(front + back, trans)


class TestCropMath(unittest.TestCase):
    def test_wap_first_segment_wap_decode_strips_ref(self):
        start, length, note, middle = sqr._sqr_crop_plan(
            "WanAnimatePlus AnimateEmbeds", 69, False, False, 3,
            has_ref_images=True)
        self.assertEqual(start, 0)
        self.assertEqual(length, 69)
        self.assertEqual(middle, 72)
        self.assertIn("WAP Decode剥参考图", note)

    def test_wap_first_segment_no_ref(self):
        start, length, _, middle = sqr._sqr_crop_plan(
            "WanAnimatePlus AnimateEmbeds", 69, False, False, 3,
            has_ref_images=False)
        self.assertEqual(start, 0)
        self.assertEqual(length, 69)
        self.assertEqual(middle, 72)

    def test_wap_ref_from_segment_list(self):
        start, length, note, middle = sqr._sqr_crop_plan(
            "WanAnimatePlus AnimateEmbeds", 69, False, False, 3,
            has_ref_images=False, ref_images_list=["a.png"])
        self.assertEqual(start, 0)
        self.assertEqual(length, 69)
        self.assertEqual(middle, 72)
        self.assertIn("WAP Decode剥参考图", note)

    def test_wap_middle_segment_decode_strip(self):
        for limit in (69, 65):
            start, length, _, middle = sqr._sqr_crop_plan(
                "WanAnimatePlus AnimateEmbeds", limit, True, False, 3,
                frame_window_size=77, has_start_ref=False, has_ref_images=False)
            self.assertEqual(start, 0)
            self.assertEqual(length, limit)
            self.assertEqual(middle, limit + 3)

    def test_wap_middle_segment_with_ref_no_sqr_ref_skip(self):
        start, length, note, middle = sqr._sqr_crop_plan(
            "WanAnimatePlus AnimateEmbeds", 69, True, False, 3,
            frame_window_size=77, has_ref_images=True)
        self.assertEqual(start, 0)
        self.assertEqual(length, 69)
        self.assertIn("Decode剥21", note)
        self.assertIn("WAP Decode剥参考图", note)

    def test_wap_middle_segment_decode_strip_when_looping(self):
        start, length, note, middle = sqr._sqr_crop_plan(
            "WanAnimatePlus AnimateEmbeds", 69, True, False, 3,
            frame_window_size=60, has_start_ref=False, has_ref_images=False)
        self.assertEqual(start, 0)
        self.assertEqual(length, 69)
        self.assertEqual(middle, 72)
        self.assertIn("Decode剥21", note)

    def test_wap_looping_with_ref_only_transition_skip(self):
        start, length, note, middle = sqr._sqr_crop_plan(
            "WanAnimatePlus AnimateEmbeds", 69, True, False, 3,
            frame_window_size=60, has_ref_images=True)
        self.assertEqual(start, 0)
        self.assertEqual(length, 69)
        self.assertEqual(middle, 72)
        self.assertIn("WAP Decode剥参考图", note)
        self.assertIn("Decode剥21", note)

    def test_original_middle_segment_keeps_limit(self):
        cases = (
            ("WanVideoAnimateEmbeds", 81),
            ("WanVideoAnimateEmbeds", 125),
        )
        for class_type, limit in cases:
            trans, front, back = sqr._sqr_transition_constants(class_type)
            with self.subTest(class_type=class_type, limit=limit):
                start, length, _, middle = sqr._sqr_crop_plan(
                    class_type, limit, True, False, 3)
                self.assertEqual(length, limit)
                self.assertEqual(start, front)
                self.assertEqual(middle, length)


class TestFindAnimateEmbeds(unittest.TestCase):
    def test_finds_wan_animate_plus(self):
        prompt = {
            "1": {"class_type": "VHS_LoadVideo", "inputs": {}},
            "62": {"class_type": "WanAnimatePlus AnimateEmbeds", "inputs": {}},
        }
        self.assertEqual(sqr.find_animate_embeds_node(prompt), "62")

    def test_finds_original(self):
        prompt = {"62": {"class_type": "WanVideoAnimateEmbeds", "inputs": {}}}
        self.assertEqual(sqr.find_animate_embeds_node(prompt), "62")


class TestCalcSegments(unittest.TestCase):
    def test_segment_limits_are_4n_plus_1(self):
        segs = sqr.calc_segments(200, 4)
        self.assertEqual(len(segs), 4)
        for skip, limit in segs:
            self.assertGreater(limit, 0)
            self.assertEqual(limit % 4, 1)
            self.assertGreaterEqual(skip, 0)


class TestVideoPick(unittest.TestCase):
    def test_pick_prefers_video_only_by_default(self):
        gifs = [
            {"filename": "sqr_cut_00001-audio.mp4"},
            {"filename": "sqr_cut_00001.mp4"},
        ]
        picked = sqr._sqr_pick_video_gif(gifs)
        self.assertEqual(picked["filename"], "sqr_cut_00001.mp4")

    def test_pick_prefers_audio_for_merge(self):
        gifs = [
            {"filename": "sqr_cut_00001.mp4"},
            {"filename": "sqr_cut_00001-audio.mp4"},
        ]
        picked = sqr._sqr_pick_video_gif(gifs, prefer_audio=True)
        self.assertEqual(picked["filename"], "sqr_cut_00001-audio.mp4")


class TestMergeVideos(unittest.TestCase):
    def test_target_fps_uses_unified_filter_complex(self):
        completed = MagicMock(returncode=0, stderr="")
        with patch.object(sqr, "_sqr_video_has_audio", return_value=True), \
             patch("subprocess.run", return_value=completed) as run:
            ok = sqr.merge_videos(
                ["seg1.mp4", "seg2.mp4"],
                "merged.mp4",
                target_fps=16.0,
                pix_fmt="yuv420p10le",
                crf=8,
            )

        self.assertTrue(ok)
        self.assertEqual(run.call_count, 1)
        cmd = run.call_args.args[0]
        self.assertIn("-filter_complex", cmd)
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("fps=16", fc)
        self.assertIn("format=yuv420p10le", fc)
        self.assertIn("concat=n=2", fc)
        self.assertIn("-pix_fmt", cmd)
        self.assertIn("yuv420p10le", cmd)
        self.assertIn("-profile:v", cmd)
        self.assertIn("high10", cmd)

    def test_read_vhs_encode_opts_from_widgets_dict(self):
        prompt = {
            "99": {
                "class_type": "VHS_VideoCombine",
                "inputs": {},
                "widgets_values": {
                    "pix_fmt": "yuv420p10le",
                    "crf": 8,
                },
            }
        }
        opts = sqr._sqr_read_vhs_encode_opts(prompt, "99")
        self.assertEqual(opts["pix_fmt"], "yuv420p10le")
        self.assertEqual(opts["crf"], 8)


class TestWorkflowPatch(unittest.TestCase):
    """Verify per-segment prompt patch fields without queueing ComfyUI."""

    def test_wan_animate_plus_patch_fields(self):
        limit = 81
        skip = 0
        frame_rate = 16.0
        trans, front, _ = sqr._sqr_transition_constants("WanAnimatePlus AnimateEmbeds")

        wf = {
            "63": {
                "class_type": "VHS_LoadVideo",
                "inputs": {"force_rate": 16, "video": "ref.mp4"},
            },
            "62": {
                "class_type": "WanAnimatePlus AnimateEmbeds",
                "inputs": {"num_frames": 999, "vae": ["1", 0]},
            },
        }

        wf["63"]["inputs"]["skip_first_frames"] = skip
        wf["63"]["inputs"]["frame_load_cap"] = limit
        wf["62"]["inputs"]["num_frames"] = limit

        real_skip = skip
        audio_skip = max(0, real_skip - front)
        main_audio_skip = max(0, real_skip - trans)

        self.assertEqual(wf["62"]["inputs"]["num_frames"], limit)
        self.assertEqual(audio_skip, 0)
        self.assertEqual(main_audio_skip, 0)

        t_skip = max(0, 200 - trans)
        tv = {
            "force_rate": 16,
            "frame_load_cap": trans,
            "skip_first_frames": t_skip,
        }
        self.assertEqual(tv["frame_load_cap"], 21)
        self.assertEqual(tv["skip_first_frames"], 179)
        self.assertAlmostEqual(audio_skip / frame_rate, 0.0)


class TestLosslessTransition(unittest.TestCase):
    """L1 无损 PNG 衔接（消除段间色差）相关工具函数。"""

    def test_switch_default_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SQR_LOSSLESS_TRANSITION", None)
            self.assertTrue(sqr._sqr_lossless_transition_enabled())

    def test_switch_off_values(self):
        for v in ("0", "false", "off", "no", "FALSE", "Off"):
            with patch.dict(os.environ, {"SQR_LOSSLESS_TRANSITION": v}):
                self.assertFalse(sqr._sqr_lossless_transition_enabled())

    def test_switch_on_values(self):
        for v in ("1", "true", "on", "yes", "whatever"):
            with patch.dict(os.environ, {"SQR_LOSSLESS_TRANSITION": v}):
                self.assertTrue(sqr._sqr_lossless_transition_enabled())

    def test_trans_frames_subfolder_format(self):
        self.assertEqual(
            sqr._sqr_trans_frames_subfolder("20260530_101530_123", 2),
            "sqr_tframes_20260530_101530_123_seg2")

    def test_trans_frames_dir_matches_subfolder(self):
        import tempfile
        td = tempfile.gettempdir()
        with patch.object(sqr.folder_paths, "get_output_directory", return_value=td):
            d = sqr._sqr_trans_frames_dir("S", 3)
            self.assertEqual(os.path.basename(d), sqr._sqr_trans_frames_subfolder("S", 3))
            self.assertEqual(d, os.path.join(td, "sqr_tframes_S_seg3"))

    def test_count_dir_images(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for n in ("f_00001_.png", "f_00002_.PNG", "note.txt", "a.jpg"):
                open(os.path.join(d, n), "w").close()
            self.assertEqual(sqr._sqr_count_dir_images(d), 3)
        self.assertEqual(sqr._sqr_count_dir_images(None), 0)
        self.assertEqual(sqr._sqr_count_dir_images(os.path.join(tempfile.gettempdir(), "no_such_xyz")), 0)

    def test_resolve_trans_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "f_00001_.png"), "w").close()
            self.assertEqual(sqr._sqr_resolve_trans_dir(d), os.path.realpath(d))
            parent = os.path.dirname(d)
            with patch.object(sqr.folder_paths, "get_output_directory", return_value=parent):
                self.assertEqual(
                    sqr._sqr_resolve_trans_dir(os.path.basename(d)),
                    os.path.realpath(d))
        with patch.object(sqr.folder_paths, "get_output_directory", return_value=tempfile.gettempdir()):
            self.assertIsNone(sqr._sqr_resolve_trans_dir(""))
            self.assertIsNone(sqr._sqr_resolve_trans_dir("nonexistent_xyz_dir"))


def _print_manual_checklist():
    print("\n--- ComfyUI 手动验证清单 ---")
    print("1. 重启 ComfyUI，加载本插件最新代码")
    print("2. SQR execute=OFF → 先看分段计划是否正确")
    print("3. 分段数设为 2，execute=ON，只跑 2 段做 smoke test")
    print("4. 打开 SQR「查看日志」，确认出现:")
    print("   - AnimateEmbeds节点: [...] (WanAnimatePlus AnimateEmbeds)")
    print("   - WAP分段规则: 读n+6帧，过渡21帧，裁尾3帧→保存n帧")
    print("   - 第2段: 裁切：WAP...Decode剥21后取<limit+3>帧再存<limit>帧")
    print("5. 检查 output/ 下 sqr_cut_*_seg*.mp4 帧数是否与日志一致")
    print("6. 合并后 sqr_merged_*.mp4：第1/2段衔接处应无明显跳帧/音画错位")
    print("7. [无损衔接] output/ 下出现 sqr_tframes_<时间>_seg*/ 目录，内含末21帧PNG")
    print("8. [无损衔接] 日志出现: ✓ 过渡(无损PNG): sqr_tframes_..._segN 取21帧（零色偏衔接）")
    print("9. [关键] 第2/3段开头色温突跳应消失（本次修复目标）")
    print("10. [回退] 设环境变量 SQR_LOSSLESS_TRANSITION=0 → 切回旧 mp4 衔接")


if __name__ == "__main__":
    print(f"Testing SQR frame math from: {_ROOT}")
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    _print_manual_checklist()
    raise SystemExit(0 if result.wasSuccessful() else 1)
