#!/usr/bin/env python3
"""Fix seed_templates.sql to add NULL command_template to all existing template_jobs."""

import re

with open("seed_templates.sql", "r") as f:
    content = f.read()

# Pattern to find template_jobs INSERT statements without command_template
# Match: INSERT INTO template_jobs VALUES (..., 'prompt', 50, 300);
# Replace with: INSERT INTO template_jobs VALUES (..., 'prompt', NULL, 50, 300);

# Find all template_jobs inserts that end with: '...', 50, 300);
# and DON'T already have NULL before 50
pattern = r"(INSERT INTO template_jobs VALUES \([^)]+,\s*)'([^']*)',\s*(\d+),\s*(\d+)\s*\);"

def replacer(match):
    prefix = match.group(1)
    prompt = match.group(2)
    max_iter = match.group(3)
    timeout = match.group(4)

    # Check if this already has a command (6th comma after opening paren)
    # Count commas in prefix
    comma_count = prefix.count(',')

    # If we have 5 commas, we're missing command_template (should have 6)
    # Format: (id, stage_id, agent_type, name, prompt_template, command_template, max_iter, timeout)
    if comma_count == 4:  # id, stage_id, agent_type, name (4 commas before prompt)
        return f"{prefix}'{prompt}',\n    NULL,  -- No custom command (uses harness)\n    {max_iter}, {timeout});"
    else:
        # Already has correct number of fields
        return match.group(0)

content = re.sub(pattern, replacer, content, flags=re.MULTILINE | re.DOTALL)

with open("seed_templates.sql", "w") as f:
    f.write(content)

print("Fixed seed_templates.sql")
