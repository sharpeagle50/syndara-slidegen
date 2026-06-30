"""Diagram Generator Agent: produces matplotlib PNGs from chart descriptions."""
import json
import re
from pathlib import Path
from typing import Optional

from .base import BaseAgent
from ..tools.code_exec_tool import run_diagram_code

DIAGRAM_GEN_SYSTEM = """You are the Syndara Diagram Generator Agent. You produce matplotlib Python code that generates clear, professional diagrams.

STRICT RULES:
- You may ONLY use matplotlib and numpy. No other imports.
- Always use the Syndara color scheme: bg='#0F172A', accent='#4AD8C4', text='white', subtext='#B0BEC5'
- Figure size: figsize=(10, 6), facecolor='#0F172A'
- All axes: ax.set_facecolor('#0F172A')
- All text: color='white' or '#B0BEC5'
- Grid: ax.grid(color='#334155', alpha=0.5)
- Bars/lines/fills: use '#4AD8C4' as primary, '#3b82f6' as secondary
- Always call plt.tight_layout()
- Do NOT call plt.show(). Do NOT call plt.savefig(). The framework handles saving.
- Just set up the figure and axes.

Output only Python code, no explanation, no markdown fences.
"""


class DiagramGenAgent(BaseAgent):
    # Only code_exec — no file writes, no web access
    allowed_tool_names = ["code_exec"]
    system_prompt = DIAGRAM_GEN_SYSTEM
    # Sonnet, not the Opus default: this emits boilerplate matplotlib code (a mechanical
    # task Sonnet 5 handles well), and the rendered chart is checked downstream by Visual
    # QA's inaccurate_visual category — so a bad diagram is caught rather than shipped.
    model = "claude-sonnet-5"

    def run(self, description: str, output_path: str) -> dict:
        """
        Generate a matplotlib diagram from a text description.
        Returns {"success": True, "path": output_path} or {"success": False, "error": "..."}
        """
        user_msg = f"""Generate matplotlib Python code for this diagram:

{description}

Remember: Syndara color scheme, no plt.show(), no plt.savefig(). Output only Python code."""

        messages = [{"role": "user", "content": user_msg}]
        response = self.call(messages, max_tokens=2000)
        code = response.content[0].text if response.content else ""

        # Strip markdown fences if present
        code = re.sub(r"```python\s*", "", code)
        code = re.sub(r"```\s*", "", code).strip()

        if not code:
            return {"success": False, "error": "No code generated"}

        return run_diagram_code(code, output_path)
