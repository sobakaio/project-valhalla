import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server as app


VIDEO_WORKFLOW = {
    "395": {"class_type": "LoadImage", "inputs": {"image": "input.png"}},
    "398:376": {"class_type": "PrimitiveNode", "inputs": {"value": "old prompt"}},
    "398:339": {"class_type": "RandomNoise", "inputs": {"noise_seed": 11}},
    "398:338": {"class_type": "RandomNoise", "inputs": {"noise_seed": 12}},
    "398:362": {"class_type": "PrimitiveNode", "inputs": {"value": 5}},
    "latent": {"class_type": "EmptyLTXVLatentVideo", "inputs": {"length": ["expr", 0]}},
    "expr": {"class_type": "MathExpression", "inputs": {"values.a": ["398:362", 0]}},
    "75": {"class_type": "SaveVideo", "inputs": {"video": ["latent", 0]}},
}


class VideoWorkflowTests(unittest.TestCase):
    def test_video_profiles_share_root_directory_and_migrate_legacy_subfolder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workflows"
            legacy = root / "video"
            legacy.mkdir(parents=True)
            profile = legacy / "ltx-2-5-i2v.workflow.json"
            profile.write_text("{\"75\": {}}\n", encoding="utf-8")
            config_file = Path(temporary) / "config.json"
            config = {"comfy": {"workflows_dir": "./workflows"}}
            with patch.object(app, "load_config", return_value=(config, config_file)):
                self.assertEqual(
                    app.workflow_profile_directory({}, Path(temporary) / "database.json", "image"),
                    root,
                )
                self.assertEqual(
                    app.workflow_profile_directory({}, Path(temporary) / "database.json", "video"),
                    root,
                )

            migrated = root / profile.name
            self.assertEqual(migrated.read_text(encoding="utf-8"), "{\"75\": {}}\n")
            self.assertTrue(migrated.is_file())
            self.assertFalse(legacy.exists())

    def test_root_profiles_are_filtered_by_media_type(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workflows"
            root.mkdir()
            (root / "image.workflow.json").write_text(
                json.dumps({"save": {"class_type": "SaveImage", "inputs": {}}}),
                encoding="utf-8",
            )
            (root / "video.workflow.json").write_text(
                json.dumps(VIDEO_WORKFLOW), encoding="utf-8"
            )
            config_file = Path(temporary) / "config.json"
            config = {
                "comfy": {
                    "workflows_dir": "./workflows",
                    "media_profiles": {
                        "image": {"source": "profiles", "production": "image", "preview": "image"},
                        "video": {"source": "profiles", "production": "video"},
                    },
                }
            }
            with (
                patch.object(app, "load_config", return_value=(config, config_file)),
                patch.object(app, "detect_node_mapping", return_value={}),
            ):
                image_profiles = app.list_workflow_profiles({}, Path(temporary) / "database.json", "image")
                video_profiles = app.list_workflow_profiles({}, Path(temporary) / "database.json", "video")

            self.assertEqual([item["id"] for item in image_profiles["profiles"]], ["image"])
            self.assertEqual([item["id"] for item in video_profiles["profiles"]], ["video"])

    def test_video_capture_cannot_replace_an_image_profile_with_same_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workflows"
            root.mkdir()
            (root / "shared.workflow.json").write_text(
                json.dumps({"save": {"class_type": "SaveImage", "inputs": {}}}),
                encoding="utf-8",
            )
            config_file = Path(temporary) / "config.json"
            config = {
                "comfy": {
                    "workflows_dir": "./workflows",
                    "media_profiles": {
                        "image": {"source": "profiles", "production": "shared", "preview": "shared"},
                        "video": {"source": "profiles", "production": None},
                    },
                }
            }
            with (
                patch.object(app, "load_config", return_value=(config, config_file)),
                patch.object(app, "latest_comfy_workflow", return_value=("prompt-id", VIDEO_WORKFLOW)),
            ):
                with self.assertRaisesRegex(app.AppError, "already used by an Image workflow"):
                    app.capture_workflow_profile(
                        {}, Path(temporary) / "database.json", "shared", True, "video"
                    )

    def test_ltx_video_mapping_detects_image_prompt_seeds_duration_and_output(self):
        mapping = app.detect_video_node_mapping(VIDEO_WORKFLOW)

        self.assertEqual(mapping["image_targets"], [{"node": "395", "input": "image"}])
        self.assertEqual(mapping["prompt"], {"node": "398:376", "input": "value"})
        self.assertEqual(len(mapping["inference_seed"]), 2)
        self.assertEqual(mapping["duration"], {"node": "398:362", "input": "value"})
        self.assertEqual(mapping["output_nodes"], ["75"])

    def test_video_workflow_patch_changes_only_detected_controls(self):
        workflow = copy.deepcopy(VIDEO_WORKFLOW)
        workflow["395"]["inputs"]["keep"] = True
        workflow["398:376"]["inputs"]["keep"] = True
        mapping = app.detect_video_node_mapping(workflow)

        app.patch_video_workflow(workflow, mapping, "uploaded.png", "new motion", 99, 8)

        self.assertEqual(workflow["395"]["inputs"], {"image": "uploaded.png", "keep": True})
        self.assertEqual(workflow["398:376"]["inputs"]["value"], "new motion")
        self.assertEqual(workflow["398:339"]["inputs"]["noise_seed"], 99)
        self.assertEqual(workflow["398:338"]["inputs"]["noise_seed"], 99)
        self.assertEqual(workflow["398:362"]["inputs"]["value"], 8)

    def test_video_generation_links_output_to_source_media_id_in_filename(self):
        class Response:
            content = b"fake mp4 bytes"

            def raise_for_status(self):
                pass

            def json(self):
                return {"name": "uploaded.png", "prompt_id": "video-prompt"}

        class Session:
            def post(self, url, **_kwargs):
                return Response()

            def get(self, *_args, **_kwargs):
                return Response()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / (
                "20260911_160243_066977_random_001_production_shot_010_"
                "6909363532413516788_image_01.png"
            )
            source.write_bytes(b"source image")
            config_file = root / "config.json"
            config = {"storage": {"output_dir": "./outputs"}}
            with (
                patch.object(app, "comfy_session", return_value=(Session(), "http://comfy", 1)),
                patch.object(app, "wait_for_outputs", return_value={
                    "75": {"images": [{"filename": "render.mp4", "type": "output"}]},
                }),
                patch.object(app, "load_config", return_value=(config, config_file)),
            ):
                prompt_id, paths = app.generate_video_one(
                    {"settings": {}}, source,
                    {
                        "source": "output", "relative_path": source.name, "name": source.name,
                        "source_key": f"output:{source.name}", "generation_mode": "photoshoot",
                        "render_tier": "production", "group_index": 1, "shot": 4,
                    },
                    "camera moves", 123, 6, VIDEO_WORKFLOW,
                    app.detect_video_node_mapping(VIDEO_WORKFLOW), "run-1",
                )

            self.assertEqual(prompt_id, "video-prompt")
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].read_bytes(), b"fake mp4 bytes")
            self.assertEqual(
                paths[0].name,
                "run-1_video_from_6909363532413516788_image_01_123_video_01.mp4",
            )
            payload = app.output_payload(paths[0])
            self.assertEqual(payload["media_type"], "video")
            self.assertEqual(payload["source_media_id"], "6909363532413516788")
            self.assertEqual(payload["source_media_ref"], "6909363532413516788_image_01")
            self.assertEqual(payload["source_key"], "output:6909363532413516788_image_01")
            self.assertEqual(payload["source_image"], "6909363532413516788_image_01")
            self.assertIsNone(payload["video_prompt"])
            self.assertIsNone(payload["video_duration"])

    def test_video_gallery_lists_and_deletes_video_without_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            video = output_dir / (
                "run_video_from_6909363532413516788_image_01_123_video_01.mp4"
            )
            video.write_bytes(b"video")
            with (
                patch.object(app, "proof_directories", return_value=[("output", output_dir)]),
                patch.object(app, "output_directory", return_value=output_dir),
                patch.object(app.WEB_STATE, "jobs", {}),
            ):
                outputs = app.list_output_images()
                self.assertEqual(outputs[0]["media_type"], "video")
                self.assertEqual(outputs[0]["source_media_id"], "6909363532413516788")
                self.assertEqual(outputs[0]["source_key"], "output:6909363532413516788_image_01")
                result = app.delete_output_image(video.name)

            self.assertEqual(result["deleted"], video.name)
            self.assertFalse(video.exists())

    def test_video_job_uses_video_registry_and_enters_shared_fifo_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            source = output_dir / "frame.png"
            source.write_bytes(b"source")
            config_file = output_dir / "config.json"
            config = {"limits": {"max_jobs": 4}, "storage": {"output_dir": "."}}
            with (
                patch.object(app, "load_database", return_value=({"settings": {}}, output_dir / "database.json")),
                patch.object(app, "load_config", return_value=(config, config_file)),
                patch.object(app, "proof_directories", return_value=[("output", output_dir)]),
                patch.object(app, "workflow_source", return_value="profiles"),
                patch.object(app, "load_workflow_profile_registry", return_value={"production": "ltx-2.5"}),
                patch.object(app.threading, "Thread") as thread,
            ):
                state = app.WebState()
                payload = state.create_video_job(
                    "output", "frame.png", "subtle camera movement", 5,
                    {"source_key": "output:frame.png", "shot": 7},
                )

            self.assertEqual(payload["kind"], "video")
            self.assertEqual(payload["workflow_profile"], "ltx-2.5")
            self.assertEqual(payload["generation_mode"], "video")
            self.assertEqual(state.jobs[payload["id"]]["_video_source"]["source_key"], "output:frame.png")
            thread.assert_called_once()


if __name__ == "__main__":
    unittest.main()
