"""Load exact, versioned ambiguity prompts without switching the working tree."""

import ast
import hashlib
import subprocess


def load_main_prompt(root):
    commit = subprocess.check_output(["git", "rev-parse", "main"], cwd=root, text=True).strip()
    path = "src/adv_text2sql/mcp_servers/text2sql_tool/src/prompts.py"
    source = subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=root).decode("utf-8")
    prompt = next(ast.literal_eval(node.value) for node in ast.parse(source).body
                  if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "AMBIGUITY_PROMPT_TEMPLATE"
                          for target in node.targets))
    return prompt, {"source": "main", "commit": commit, "path": path,
                    "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
