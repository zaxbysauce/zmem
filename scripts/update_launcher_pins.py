"""Update test_launcher.js pins for the two new closeout hook entries:
#186 first-leg Claude VERBS map, #186 count 13->15, #186 no-Git VERBS
array, #187 nested count 7->9, and the stale '13 verbs' heading."""

import io

p = 'tests/test_launcher.js'
with io.open(p, 'r', encoding='utf-8', newline='') as f:
    t = f.read()

# 1. #186 first-leg Claude manifest VERBS map: UserPromptSubmit + Stop
old_map = ('        "UserPromptSubmit": ["recall", "capture-correction"],\n'
           '        "PreToolUse": ["pretool-recall"],')
assert t.count(old_map) == 1, f"map UPS: {t.count(old_map)}"
new_map = ('        "UserPromptSubmit": ["recall", "capture-correction",'
           ' "closeout-request"],\n'
           '        "PreToolUse": ["pretool-recall"],')
t = t.replace(old_map, new_map, 1)

old_stop = '        "Stop": ["reflect"],\n'
assert t.count(old_stop) == 1, f"map Stop: {t.count(old_stop)}"
t = t.replace(old_stop, '        "Stop": ["reflect", "closeout-gate"],\n', 1)

# 2. #186 count pin 13 -> 15
old_count = 'eq("#186: manifest command entry count", count, 13);'
assert t.count(old_count) == 1, f"count: {t.count(old_count)}"
t = t.replace(old_count,
              'eq("#186: manifest command entry count", count, 15);', 1)

# 3. no-Git VERBS array: insert closeout-request after capture-correction
old_a2 = ('"session-start", "recall", "capture-correction", "pretool-recall",')
assert t.count(old_a2) == 1, f"arr2: {t.count(old_a2)}"
t = t.replace(old_a2,
    '"session-start", "recall", "capture-correction", "closeout-request",\n'
    '        "pretool-recall",', 1)

# 4. no-Git VERBS array: insert closeout-gate after reflect
old_a3 = ('        "reflect", "subagent-recall", "subagent-reflect", "precompact",')
assert t.count(old_a3) == 1, f"arr3: {t.count(old_a3)}"
t = t.replace(old_a3,
    '        "reflect", "closeout-gate", "subagent-recall",\n'
    '        "subagent-reflect", "precompact",', 1)

# 5. #187 nested count 7 -> 9
old_187 = 'eq("#187: manifest nested entry count", entries.length, 7);'
assert t.count(old_187) == 1, f"187: {t.count(old_187)}"
t = t.replace(old_187,
    'eq("#187: manifest nested entry count", entries.length, 9);', 1)

# 6. stale heading
t = t.replace('"[#186] 13 verbs without Git on PATH"',
              '"[#186] 15 verbs without Git on PATH"', 1)

with io.open(p, 'w', encoding='utf-8', newline='') as f:
    f.write(t)
print('test_launcher.js pins updated')
