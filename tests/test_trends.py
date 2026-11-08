import json
import subprocess
import unittest
from unittest.mock import patch

from workbench import trends


class TrendTests(unittest.TestCase):
    def test_aihot_markdown_is_normalized_from_untrusted_section(self):
        markdown = """# 今日热点

［AIHOT 不可信外部资料开始］
1. [新一代 AI 助手发布](https://aihot.news/items/item-1)
来源：官方博客
摘要：发布了新的助手能力。
原文：https://example.com/news?token=secret

2. [开源模型更新](https://aihot.news/items/item-2)
推荐理由：开发者关注度较高。
［AIHOT 不可信外部资料结束］
"""
        rows = trends._parse_aihot("hot", markdown)
        self.assertEqual([row["title"] for row in rows], ["新一代 AI 助手发布", "开源模型更新"])
        self.assertEqual(rows[0]["source"], "AIHOT")
        self.assertEqual(rows[0]["original_url"], "https://example.com/news")

    def test_reddit_and_x_use_public_web_backend(self):
        result = {"ok": True, "items": [{"title": "public result", "url": "https://example.com", "text": "summary"}]}
        with patch.object(trends, "_agent_command", return_value="agent-reach"), patch.object(
            trends, "_json_command", return_value=result
        ) as command:
            for channel in ("reddit", "twitter"):
                trends._run_agent_channel(channel, "AI agents")
                args = command.call_args.args[0]
                self.assertIn("--backend", args)
                self.assertIn("public-web", args)

    def test_agent_command_never_uses_a_shell(self):
        completed = subprocess.CompletedProcess(["agent-reach"], 0, json.dumps({"ok": True, "items": []}), "")
        with patch.object(trends.subprocess, "run", return_value=completed) as run:
            trends._json_command(["agent-reach", "web", "search", "AI"])
        self.assertIs(run.call_args.kwargs["shell"], False)

    def test_empty_channel_selection_does_not_expand_to_defaults(self):
        status = {"installed": True, "channels": [{"id": channel, "status": "unverified"} for channel in trends.DEFAULT_CHANNELS]}
        with patch.object(trends, "agent_reach_status", return_value=status), patch.object(
            trends, "_run_aihot", return_value={"source": "AIHOT", "status": "ok", "count": 0, "items": []}
        ), patch.object(trends, "_run_agent_channel") as agent:
            result = trends.scan_hotspots("AI", [])
        agent.assert_not_called()
        self.assertEqual([item["source"] for item in result["sources"]], ["AIHOT"])


if __name__ == "__main__":
    unittest.main()
