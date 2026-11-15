import json
import os
import random
import subprocess
import sys
import unittest

from stacksense import engine

from tests.helpers import SRC, public, random_scenario


def dump(out):
    return json.dumps(out, indent=2, ensure_ascii=False, sort_keys=False)


def shuffled(obj, rng):
    if isinstance(obj, dict):
        items = list(obj.items())
        rng.shuffle(items)
        return {k: shuffled(v, rng) for k, v in items}
    return obj


class Determinism(unittest.TestCase):
    def test_same_input_same_bytes(self):
        for n in ("P1", "P2", "P3", "P4", "P5", "P6"):
            raw = public(n)
            self.assertEqual(dump(engine.run(raw)), dump(engine.run(raw)), n)

    def test_key_order_does_not_matter(self):
        rng = random.Random(7)
        for n in ("P2", "P4", "P6"):
            raw = public(n)
            self.assertEqual(dump(engine.run(raw)), dump(engine.run(shuffled(raw, rng))), n)
        for _ in range(20):
            raw = random_scenario(rng)
            self.assertEqual(dump(engine.run(raw)), dump(engine.run(shuffled(raw, rng))))

    def test_llm_switch_changes_nothing(self):
        for n in ("P2", "P5"):
            raw = public(n)
            self.assertEqual(dump(engine.run(raw, llm="on")), dump(engine.run(raw, llm="off")))
            self.assertFalse(engine.run(raw, llm="on")["llm"]["enabled"])

    def test_hash_seed_does_not_matter(self):
        code = ("import json, glob; from stacksense import engine; "
                "print(json.dumps([engine.run(json.load(open(f, encoding='utf-8-sig'))) "
                "for f in sorted(glob.glob('scenarios/public/*.json') + glob.glob('scenarios/edge/E1*.json'))], "
                "sort_keys=False))")
        outs = []
        for seed in ("0", "1"):
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1")
            res = subprocess.run([sys.executable, "-c", code], cwd=SRC, env=env, capture_output=True, check=True)
            outs.append(res.stdout)
        self.assertEqual(outs[0], outs[1])


if __name__ == "__main__":
    unittest.main()
