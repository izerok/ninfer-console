#!/usr/bin/env python3
"""Unit tests for console_core (ninfer-console, generic edition).

All fixtures are synthetic: the core ships no deployment's models,
images, presets or limits, so the tests build their own library,
their own presets and their own runtime limits — exactly like any
operator's environment would supply at runtime.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import console_core as cc


MODELS_JSON = """
{"models": [
  {"id": "full-model", "name": "Full Model", "file": "full-model.ninfer",
   "size_gb": 10.0, "default_spec": "dflash2",
   "components": {"vision": true, "mtp": true, "dflash2": true}},
  {"id": "text-model", "name": "Text Model", "file": "text-model.ninfer",
   "size_gb": 5.0, "default_spec": "mtp",
   "components": {"vision": false, "mtp": true, "dflash2": false}},
  {"id": "nvfp4-model", "name": "NVFP4 Model", "file": "nvfp4-model.ninfer",
   "size_gb": 20.0, "requires": ["nvfp4"],
   "components": {"vision": true, "mtp": false, "dflash2": true}},
  {"id": "mystery-model", "name": "Mystery Model", "file": "mystery-model.ninfer"}
]}
"""

DISCOVERED = {"full-model.ninfer": 10_500_000_000, "stray.ninfer": 2_000_000_000}


def library():
    entries, problems = cc.load_models_json(MODELS_JSON)
    assert not problems, problems
    return cc.merge_models(entries, DISCOVERED)


def codes(issues):
    return [i["code"] for i in issues]


def cfg(**over):
    base = cc.blank_config()
    base.update({"model": "full-model", "image": "registry.example.com/team/ninfer:1.0",
                 "max_context": 65536, "kv_capacity": "auto"})
    base.update(over)
    return base


class TestBlankConfig(unittest.TestCase):
    def test_blank_is_neutral(self):
        blank = cc.blank_config()
        self.assertIsNone(blank["model"])
        self.assertIsNone(blank["image"])
        self.assertIsNone(blank["host_cache_mib"])
        self.assertFalse(blank["vision"])
        self.assertEqual(blank["spec"], "none")

    def test_blank_only_missing_model(self):
        errors, warnings = cc.validate_config(cc.blank_config(), models=library())
        self.assertEqual(codes(errors), ["model_required"])

    def test_valid_config_passes(self):
        errors, warnings = cc.validate_config(cfg(spec="dflash2", draft_tokens=4), models=library())
        self.assertEqual(errors, [])


class TestLoaders(unittest.TestCase):
    def test_models_json_ok(self):
        entries, problems = cc.load_models_json(MODELS_JSON)
        self.assertEqual(problems, [])
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0]["source"], "file")

    def test_models_json_corrupt_never_raises(self):
        entries, problems = cc.load_models_json("{not json")
        self.assertEqual(entries, [])
        self.assertTrue(problems)
        entries, problems = cc.load_models_json('{"models": "nope"}')
        self.assertEqual(entries, [])
        self.assertTrue(problems)

    def test_models_json_skips_bad_entries(self):
        text = """{"models": [
          {"name": "no id or file"},
          {"id": "ok", "file": "ok.ninfer"},
          {"id": "ok", "file": "dupe.ninfer"},
          {"id": "badcomp", "file": "b.ninfer", "components": "yes"},
          {"id": "badreq", "file": "c.ninfer", "requires": ["warp-drive"]}
        ]}"""
        entries, problems = cc.load_models_json(text)
        ids = [e["id"] for e in entries]
        self.assertEqual(ids, ["ok", "badcomp", "badreq"])
        self.assertIsNone(entries[1]["components"])
        self.assertEqual(entries[2]["requires"], [])
        self.assertGreaterEqual(len(problems), 4)

    def test_id_defaults_to_file_stem(self):
        entries, _ = cc.load_models_json('{"models": [{"file": "some-model.ninfer"}]}')
        self.assertEqual(entries[0]["id"], "some-model")

    def test_presets_sanitized(self):
        text = """{"presets": [
          {"id": "p1", "label": "My preset",
           "config": {"model": "full-model", "max_context": 4096,
                      "bogus_key": 1, "vision": "yes"}},
          {"label": "no id"}
        ]}"""
        presets, problems = cc.load_presets_json(text)
        self.assertEqual(len(presets), 1)
        self.assertEqual(presets[0]["config"]["max_context"], 4096)
        self.assertNotIn("bogus_key", presets[0]["config"])
        self.assertFalse(presets[0]["config"]["vision"])  # bad type dropped to blank
        self.assertTrue(problems)

    def test_sanitize_config(self):
        out = cc.sanitize_config({"max_context": "wide", "spec": "mtp", "nope": 1})
        self.assertEqual(out["spec"], "mtp")
        self.assertEqual(out["max_context"], cc.blank_config()["max_context"])


class TestMerge(unittest.TestCase):
    def test_merge_states(self):
        models = library()
        by_id = {m["id"]: m for m in models}
        self.assertEqual(by_id["full-model"]["source"], "both")
        self.assertAlmostEqual(by_id["full-model"]["size_gb"], 10.5)  # measured wins
        self.assertEqual(by_id["text-model"]["source"], "file")
        self.assertEqual(by_id["text-model"]["size_gb"], 5.0)  # registered value kept
        self.assertEqual(by_id["stray"]["source"], "discovered")
        self.assertIsNone(by_id["stray"]["components"])
        self.assertAlmostEqual(by_id["stray"]["size_gb"], 2.0)

    def test_available_specs(self):
        models = library()
        by_id = {m["id"]: m for m in models}
        self.assertEqual(cc.available_specs(by_id["text-model"]), ["none", "mtp"])
        # unknown components -> everything offered, validation warns
        self.assertEqual(cc.available_specs(by_id["stray"]), ["none", "dflash2", "mtp"])
        self.assertEqual(cc.available_specs(None), ["none", "dflash2", "mtp"])


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.models = library()

    def test_known_conflicts_are_errors(self):
        errors, _ = cc.validate_config(cfg(model="text-model", vision=True), models=self.models)
        self.assertIn("vision_not_in_model", codes(errors))
        errors, _ = cc.validate_config(cfg(model="text-model", spec="dflash2"), models=self.models)
        self.assertIn("spec_not_in_model", codes(errors))

    def test_unknown_capabilities_warn_not_error(self):
        errors, warnings = cc.validate_config(
            cfg(model="mystery-model", vision=True, spec="mtp"), models=self.models)
        self.assertEqual(errors, [])
        self.assertIn("vision_unverified", codes(warnings))
        self.assertIn("spec_unverified", codes(warnings))

    def test_nvfp4_requirement(self):
        errors, _ = cc.validate_config(cfg(model="nvfp4-model", image_nvfp4="no"), models=self.models)
        self.assertIn("nvfp4_needs_capable_image", codes(errors))
        errors, warnings = cc.validate_config(cfg(model="nvfp4-model", image_nvfp4="unknown"), models=self.models)
        self.assertNotIn("nvfp4_needs_capable_image", codes(errors))
        self.assertIn("nvfp4_image_unverified", codes(warnings))
        errors, warnings = cc.validate_config(cfg(model="nvfp4-model", image_nvfp4="yes"), models=self.models)
        self.assertNotIn("nvfp4_image_unverified", codes(warnings))

    def test_dynamic_host_limit(self):
        over = cfg(host_cache_mib=8192)
        errors, _ = cc.validate_config(over, models=self.models, limits={"host_cache_max_mib": 4096})
        self.assertIn("host_cache_over_limit", codes(errors))
        errors, _ = cc.validate_config(over, models=self.models, limits={"host_cache_max_mib": 16384})
        self.assertNotIn("host_cache_over_limit", codes(errors))
        errors, _ = cc.validate_config(over, models=self.models, limits=None)
        self.assertNotIn("host_cache_over_limit", codes(errors))
        errors, _ = cc.validate_config(over, models=self.models)
        self.assertNotIn("host_cache_over_limit", codes(errors))

    def test_dynamic_vram_budget(self):
        _, warnings = cc.validate_config(cfg(), models=self.models, limits={"vram_budget_gb": 8.0})
        self.assertIn("model_too_large", codes(warnings))
        _, warnings = cc.validate_config(cfg(), models=self.models, limits={"vram_budget_gb": 64.0})
        self.assertNotIn("model_too_large", codes(warnings))
        _, warnings = cc.validate_config(cfg(), models=self.models)
        self.assertNotIn("model_too_large", codes(warnings))

    def test_structural_checks(self):
        errors, _ = cc.validate_config(cfg(unknown_key=1), models=self.models)
        self.assertIn("unknown_field", codes(errors))
        errors, _ = cc.validate_config(cfg(max_context="wide"), models=self.models)
        self.assertIn("bad_type", codes(errors))
        errors, _ = cc.validate_config(cfg(kv_dtype="q8"), models=self.models)
        self.assertIn("bad_choice", codes(errors))
        errors, _ = cc.validate_config(cfg(max_context=None), models=self.models)
        self.assertIn("null_not_allowed", codes(errors))
        errors, _ = cc.validate_config(cfg(model="ghost"), models=self.models)
        self.assertIn("unknown_model", codes(errors))
        errors, _ = cc.validate_config(cfg(image="not a ref!!"), models=self.models)
        self.assertIn("bad_image_ref", codes(errors))
        errors, _ = cc.validate_config("not-a-dict", models=self.models)
        self.assertIn("not_a_config", codes(errors))

    def test_mode_and_spec_rules(self):
        errors, _ = cc.validate_config(cfg(host_kv_mib=1024), models=self.models)  # hybrid default
        self.assertIn("hybrid_with_catalog_param", codes(errors))
        errors, _ = cc.validate_config(
            cfg(cache_mode="catalog", host_cache_mib=1024, host_kv_mib=None), models=self.models)
        self.assertIn("catalog_with_hybrid_param", codes(errors))
        errors, _ = cc.validate_config(cfg(spec="mtp", draft_tokens=4), models=self.models)
        self.assertIn("draft_tokens_with_non_dflash", codes(errors))
        errors, _ = cc.validate_config(cfg(vision=False, vision_residency="gpu"), models=self.models)
        self.assertIn("residency_without_vision", codes(errors))


class TestCommandRoundTrip(unittest.TestCase):
    def setUp(self):
        self.models = library()

    def test_round_trip(self):
        original = cfg(spec="dflash2", draft_tokens=4, vision=True,
                       host_cache_mib=2048, preserve_thinking=True)
        parsed, unparsed, model_file = cc.parse_command(cc.build_command(original), models=self.models)
        self.assertEqual(unparsed, [])
        self.assertEqual(model_file, "model.ninfer")
        for key in ("max_context", "kv_capacity", "kv_dtype", "cache_mode",
                    "host_cache_mib", "spec", "draft_tokens", "vision",
                    "preserve_thinking", "max_concurrency"):
            self.assertEqual(parsed[key], original[key], key)

    def test_parse_resolves_model_file(self):
        cmd = ["serve", "/models/full-model.ninfer", "--max-context", "8192"]
        parsed, unparsed, model_file = cc.parse_command(cmd, models=self.models)
        self.assertEqual(parsed["model"], "full-model")
        self.assertEqual(parsed["max_context"], 8192)
        self.assertEqual(model_file, "full-model.ninfer")

    def test_parse_catalog_shape(self):
        cmd = ["serve", "/models/text-model.ninfer", "--kv-capacity", "524288",
               "--host-kv-mib", "4096", "--spec", "mtp"]
        parsed, unparsed, _ = cc.parse_command(cmd, models=self.models)
        self.assertEqual(parsed["cache_mode"], "catalog")
        self.assertEqual(parsed["host_kv_mib"], 4096)
        self.assertEqual(parsed["spec"], "mtp")
        errors, _ = cc.validate_config(parsed, models=self.models)
        self.assertEqual(errors, [])

    def test_parse_robustness(self):
        parsed, unparsed, mf = cc.parse_command(None)
        self.assertIsNone(mf)
        parsed, unparsed, mf = cc.parse_command("serve --vision")
        self.assertIn("not a list", unparsed[0])
        parsed, unparsed, mf = cc.parse_command(
            ["serve", "/models/model.ninfer", "--bogus", "1", "--max-context", "abc"])
        self.assertIn("--bogus", unparsed)
        self.assertIn("--max-context", unparsed)
        parsed, unparsed, mf = cc.parse_command(["serve", "/models/model.ninfer", "--api-key", "s3cret"])
        self.assertTrue(parsed["api_key_set"])
        self.assertNotIn("s3cret", str(parsed))


class TestPreviewAndDiff(unittest.TestCase):
    def setUp(self):
        self.models = library()

    def test_compose_preview(self):
        text = cc.build_compose_preview(cfg(), models=self.models)
        self.assertIn("registry.example.com/team/ninfer:1.0", text)
        self.assertIn("full-model.ninfer", text)
        self.assertIn("container_name: ninfer", text)
        text = cc.build_compose_preview(cc.blank_config(), models=self.models)
        self.assertIn("<image not set>", text)
        self.assertIn("<model file unknown>", text)

    def test_diff_labels(self):
        old = cfg()
        new = cfg(model="text-model", spec="mtp")
        diff = cc.diff_config(old, new, models=self.models)
        keys = [d["key"] for d in diff]
        self.assertEqual(keys, ["model", "spec"])
        self.assertEqual(diff[0]["new_label"], "Text Model")


if __name__ == "__main__":
    unittest.main()
