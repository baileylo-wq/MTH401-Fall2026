#!/usr/bin/env python3
"""
LaTeX worksheet to PreTeXt worksheet converter for JP-style worksheets.
Usage: python jp_convert.py filename.tex
Output: filename.ptx (same directory as input)

Source conventions:
  - Two bold lines at top: first is a course label (ignored); second is the title.
  - Outer \\begin{enumerate}: each \\item is an exercise.
    - No inner enumerate: exercise > statement, workspace on exercise.
    - Inner enumerate: exercise > introduction + tasks, workspace on tasks.
"""

import sys
import re
from pathlib import Path
import xml.etree.ElementTree as ET


# ── Low-level helpers ─────────────────────────────────────────────────────────

def strip_preamble(tex):
    m = re.search(r'\\begin\{document\}', tex)
    return tex[m.end():] if m else tex


def find_env(text, env, start=0):
    """
    Find first \\begin{env}...\\end{env} at or after `start`, tracking nesting.
    Returns (begin_pos, end_pos, inner_text) or None.
    begin_pos is the index of '\\', end_pos is just past '}'.
    """
    begin_tag = f'\\begin{{{env}}}'
    end_tag   = f'\\end{{{env}}}'
    pos = text.find(begin_tag, start)
    if pos == -1:
        return None
    depth = 1
    i = pos + len(begin_tag)
    while i < len(text) and depth > 0:
        if text[i:].startswith(begin_tag):
            depth += 1
            i += len(begin_tag)
        elif text[i:].startswith(end_tag):
            depth -= 1
            if depth == 0:
                return (pos, i + len(end_tag), text[pos + len(begin_tag):i])
            i += len(end_tag)
        else:
            i += 1
    return None


def split_items(text):
    """
    Split the inner content of an enumerate/itemize into top-level \\item chunks.
    Nested enumerate/itemize are passed through intact inside their parent item.
    Returns a list of strings (content after \\item, without the \\item itself).
    """
    items = []
    current = []
    depth = 0
    i = 0
    while i < len(text):
        if text[i:].startswith(r'\begin{enumerate}'):
            depth += 1
            current.append(r'\begin{enumerate}')
            i += len(r'\begin{enumerate}')
        elif text[i:].startswith(r'\begin{itemize}'):
            depth += 1
            current.append(r'\begin{itemize}')
            i += len(r'\begin{itemize}')
        elif text[i:].startswith(r'\end{enumerate}'):
            depth -= 1
            current.append(r'\end{enumerate}')
            i += len(r'\end{enumerate}')
        elif text[i:].startswith(r'\end{itemize}'):
            depth -= 1
            current.append(r'\end{itemize}')
            i += len(r'\end{itemize}')
        elif text[i:].startswith(r'\item') and depth == 0:
            chunk = ''.join(current).strip()
            if chunk:
                items.append(chunk)
            current = []
            i += len(r'\item')
            # skip optional label e.g. \item[a)]
            j = i
            while j < len(text) and text[j] in ' \t':
                j += 1
            if j < len(text) and text[j] == '[':
                end = text.find(']', j)
                if end != -1:
                    i = end + 1
        else:
            current.append(text[i])
            i += 1
    chunk = ''.join(current).strip()
    if chunk:
        items.append(chunk)
    return items


# ── Title extraction ──────────────────────────────────────────────────────────

_BF_RE = re.compile(
    r'\{\\bf\s*\{?([^{}]+?)\}?\}'  # {\bf text} or {\bf{text}}
    r'|\\textbf\{([^}]+)\}'        # \textbf{text}
)

def extract_second_bf(text):
    """
    Skip the first {\\bf ...} / \\textbf{...} (course label) and use the second
    as the worksheet title.  Both are removed from the returned body text.
    Returns (title_or_None, cleaned_text).
    """
    matches = list(_BF_RE.finditer(text))
    if not matches:
        return None, text
    if len(matches) == 1:
        m = matches[0]
        title = (m.group(1) or m.group(2)).strip()
        return title, text[:m.start()] + text[m.end():]
    m1, m2 = matches[0], matches[1]
    title = (m2.group(1) or m2.group(2)).strip()
    result = text[:m1.start()] + text[m1.end():m2.start()] + text[m2.end():]
    return title, result


# ── Workspace extraction ──────────────────────────────────────────────────────

def extract_workspace(item_text):
    """
    Find and remove workspace-related commands from item_text.
    Priority: \\vs{X} → Xin, \\vss{X} → Xin, \\vspace{X} → X, \\vfill → 1in.
    Removes ALL occurrences of workspace commands; returns (dim_or_None, cleaned_text).
    """
    workspace = None

    # \vs{X} → workspace="Xin"
    m = re.search(r'\\vs\{([0-9.]+)\}', item_text)
    if m and workspace is None:
        workspace = m.group(1).strip() + 'in'
    item_text = re.sub(r'\\vs\{[^}]+\}', '', item_text)

    # \vss{X} → workspace="Xin"  (preamble macro: \vspace*{#1in})
    m = re.search(r'\\vss\{([0-9.]+)\}', item_text)
    if m and workspace is None:
        workspace = m.group(1).strip() + 'in'
    item_text = re.sub(r'\\vss\{[^}]+\}', '', item_text)

    # \vspace*{X} or \vspace{X}
    m = re.search(r'\\vspace\*?\{([^}]+)\}', item_text)
    if m and workspace is None:
        ws = m.group(1).strip()
        if ws.endswith('pt'):
            ws = f'{float(ws[:-2]) / 72:.2f}in'
        workspace = ws
    item_text = re.sub(r'\\vspace\*?\{[^}]+\}', '', item_text)

    # \vfill → 1in
    if r'\vfill' in item_text:
        if workspace is None:
            workspace = '1in'
        item_text = item_text.replace(r'\vfill', '')

    return workspace, item_text


# ── Text → XML conversion ─────────────────────────────────────────────────────

def _escape_xml(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _escape_math(math):
    # In PreTeXt, & in math (e.g. array alignment) is written as \amp
    math = math.replace('&', r'\amp ').replace('<', '&lt;').replace('>', '&gt;')
    math = re.sub(r'\\hspace\*?\{[^}]+\}', '~~~', math)
    return math


def convert_text(text):
    """
    Convert a LaTeX text fragment (no enumerate/itemize blocks) to an XML string.
    Handles: math ($/$$ → <m>/<md>), \\bf/\\textbf → <term>, \\emph/\\em → <em>,
    \\url → <url>, and removes LaTeX layout noise.
    Returns a raw XML fragment (no wrapping element).
    """
    # 0. Extract eqnarray* → <md><mrow>...</mrow>...</md>
    eqnarr = {}
    def save_eqnarr(m):
        k = f'\x00A{len(eqnarr)}\x00'
        rows = []
        for line in re.split(r'\\\\', m.group(1)):
            line = line.strip()
            if line:
                rows.append(f'<mrow>{_escape_math(line)}</mrow>')
        eqnarr[k] = '<md>\n' + '\n'.join(rows) + '\n</md>'
        return k
    text = re.sub(r'\\begin\{eqnarray\*\}(.+?)\\end\{eqnarray\*\}',
                  save_eqnarr, text, flags=re.DOTALL)

    # 1. Protect display math $$ ... $$ and \[ ... \]
    dmath = {}
    def save_dmath(m):
        k = f'\x00D{len(dmath)}\x00'
        dmath[k] = _escape_math(m.group(1))
        return k
    text = re.sub(r'\$\$(.+?)\$\$', save_dmath, text, flags=re.DOTALL)
    text = re.sub(r'\\\[(.+?)\\\]', save_dmath, text, flags=re.DOTALL)

    # 2. Protect inline math $ ... $
    imath = {}
    def save_imath(m):
        k = f'\x00I{len(imath)}\x00'
        imath[k] = _escape_math(m.group(1))
        return k
    text = re.sub(r'\$(.+?)\$', save_imath, text, flags=re.DOTALL)

    # 3. Escape XML specials in remaining plain text
    text = _escape_xml(text)

    # 4. \url{...} → <url href="...">...</url>
    text = re.sub(r'\\url\{([^}]+)\}', r'<url href="https://\1">\1</url>', text)

    # 5. {\bf{text}} / {\bf text} / \textbf{text} → <term>text</term>
    text = re.sub(
        r'\{\\bf\s*\{?([^{}]+?)\}?\}|\\textbf\{([^}]+)\}',
        lambda m: f'<term>{(m.group(1) or m.group(2)).strip()}</term>',
        text
    )

    # 6. \emph{text} / {\em text} → <em>text</em>
    text = re.sub(r'\\emph\{([^}]+)\}', r'<em>\1</em>', text)
    text = re.sub(r'\{\\em\s+([^}]+)\}', r'<em>\1</em>', text)

    # 6b. {\tt text} / \tt{text} → <c>text</c>
    text = re.sub(r'\{\\tt\s+([^}]+)\}', r'<c>\1</c>', text)
    text = re.sub(r'\\tt\{([^}]+)\}', r'<c>\1</c>', text)

    # 7. Remove LaTeX layout/noise commands
    for cmd in [r'\noindent', r'\bigskip', r'\medskip', r'\smallskip',
                r'\newpage', r'\vfill', r'\hfill', r'\\']:
        text = text.replace(cmd, '')
    text = re.sub(r'\\hspace\*?\{[^}]+\}', '', text)
    text = re.sub(r'\\input\{[^}]+\}', '', text)
    text = re.sub(r'\\input\s+\S+', '', text)
    text = re.sub(r'\\vspace\*?\{[^}]+\}', '', text)
    text = re.sub(r'\\vss\{[^}]+\}', '', text)
    text = re.sub(r'\\vs\{[^}]+\}', '', text)

    # 8. Restore math placeholders
    for k, v in eqnarr.items():
        text = text.replace(k, v)
    for k, v in dmath.items():
        text = text.replace(k, f'<md>{v}</md>')
    for k, v in imath.items():
        text = text.replace(k, f'<m>{v}</m>')

    return text


def _make_paras(text, indent):
    """Split converted XML text into <p> elements on blank lines."""
    xml = convert_text(text)
    chunks = [c.strip() for c in re.split(r'\n\s*\n', xml)]
    return [f'{indent}<p>{c}</p>' for c in chunks if c]


def _is_noise(text):
    """Return True if text is empty/whitespace after stripping LaTeX layout commands."""
    t = text
    for cmd in [r'\noindent', r'\bigskip', r'\medskip', r'\smallskip',
                r'\newpage', r'\vfill', r'\hfill', r'\\']:
        t = t.replace(cmd, '')
    t = re.sub(r'\\hspace\*?\{[^}]+\}', '', t)
    t = re.sub(r'\\input\{[^}]+\}', '', t)
    t = re.sub(r'\\input\s+\S+', '', t)
    return not t.strip()


def convert_itemize(inner, indent):
    """Convert itemize inner content to a <ul> XML string."""
    lines = [f'{indent}<ul>']
    for item in split_items(inner):
        content = convert_text(item.strip())
        lines.append(f'{indent}  <li><p>{content.strip()}</p></li>')
    lines.append(f'{indent}</ul>')
    return '\n'.join(lines)


def text_to_xml(text, indent):
    """
    Convert raw LaTeX text to XML lines.
    - \\begin{itemize} → <ul>
    - \\begin{center} containing \\includegraphics → raw LaTeX in <p> for human review
    - Prose → <p> elements
    """
    result = []
    remaining = text

    while remaining.strip():
        center_env = find_env(remaining, 'center')
        itemize_env = find_env(remaining, 'itemize')

        # Pick whichever comes first
        if center_env and (itemize_env is None or center_env[0] < itemize_env[0]):
            next_type, next_env = 'center', center_env
        elif itemize_env:
            next_type, next_env = 'itemize', itemize_env
        else:
            result.extend(_make_paras(remaining, indent))
            break

        begin_pos, end_pos, inner = next_env
        before = remaining[:begin_pos]
        if before.strip():
            result.extend(_make_paras(before, indent))

        if next_type == 'center' and r'\includegraphics' in inner:
            raw = _escape_xml(remaining[begin_pos:end_pos].strip())
            result.append(f'{indent}<p>{raw}</p>')
        elif next_type == 'center':
            # No image — strip the markers and process contents normally
            result.extend(_make_paras(inner, indent))
        else:
            result.append(convert_itemize(inner, indent))

        remaining = remaining[end_pos:]

    return '\n'.join(result)


# ── PreTeXt element builders ──────────────────────────────────────────────────

def build_objectives(tcb_inner):
    """Build <objectives> from the content inside a tcolorbox environment."""
    env = find_env(tcb_inner, 'itemize')
    lines = ['  <objectives>']

    if env is None:
        intro_xml = convert_text(tcb_inner.strip())
        lines += ['    <introduction>', f'      <p>{intro_xml.strip()}</p>', '    </introduction>']
    else:
        begin_pos, end_pos, list_inner = env
        before = tcb_inner[:begin_pos].strip()
        if before:
            intro_xml = convert_text(before)
            lines += ['    <introduction>', f'      <p>{intro_xml.strip()}</p>', '    </introduction>']
        lines.append('    <ul>')
        for item in split_items(list_inner):
            content = convert_text(item.strip())
            lines.append(f'      <li><p>{content.strip()}</p></li>')
        lines.append('    </ul>')

    lines.append('  </objectives>')
    return '\n'.join(lines)


def build_introduction(text):
    """Build a <introduction> element at worksheet level."""
    lines = ['  <introduction>']
    lines.append(text_to_xml(text, indent='    '))
    lines.append('  </introduction>')
    return '\n'.join(lines)


def build_exercise(item_text):
    """
    Build an <exercise> element from one outer enumerate item.
    Simple (no inner enumerate): exercise > statement.
    With inner enumerate: exercise > introduction + tasks + conclusion.
    workspace is extracted only from text outside any inner enumerate.
    """
    env = find_env(item_text, 'enumerate')

    if env is None:
        # Simple exercise: workspace comes from the full item text
        workspace, item_text = extract_workspace(item_text)
        workspace = workspace or '1in'
        ws_attr = f' workspace="{workspace}"'
        content = text_to_xml(item_text.strip(), indent='      ')
        return '\n'.join([
            f'  <exercise{ws_attr}>',
            '    <statement>',
            content,
            '    </statement>',
            '  </exercise>',
        ])

    # Exercise with inner enumerate → tasks
    # Only look for exercise-level workspace outside the inner enumerate
    begin_pos, end_pos, inner_enum = env
    intro_text      = item_text[:begin_pos]
    conclusion_text = item_text[end_pos:]

    workspace, intro_text = extract_workspace(intro_text)
    if workspace is None:
        workspace, conclusion_text = extract_workspace(conclusion_text)
    else:
        _, conclusion_text = extract_workspace(conclusion_text)

    ws_attr = f' workspace="{workspace}"' if workspace else ''
    intro_text = intro_text.strip()

    # Strip layout noise from conclusion before checking emptiness
    conclusion_text = re.sub(r'\\newpage', '', conclusion_text).strip()

    tasks = split_items(inner_enum)

    lines = [f'  <exercise{ws_attr}>']

    if intro_text:
        lines.append('    <introduction>')
        lines.append(text_to_xml(intro_text, indent='      '))
        lines.append('    </introduction>')

    for task_raw in tasks:
        task_ws, task_raw = extract_workspace(task_raw)
        task_ws = task_ws or '1in'
        task_ws_attr = f' workspace="{task_ws}"'
        task_content = text_to_xml(task_raw.strip(), indent='        ')
        lines += [
            f'    <task{task_ws_attr}>',
            '      <statement>',
            task_content,
            '      </statement>',
            '    </task>',
        ]

    if conclusion_text:
        lines.append('    <conclusion>')
        lines.append(text_to_xml(conclusion_text, indent='      '))
        lines.append('    </conclusion>')

    lines.append('  </exercise>')
    return '\n'.join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 2:
        print('Usage: python jp_convert.py filename.tex')
        sys.exit(1)

    input_path = Path(sys.argv[1])
    if not input_path.exists():
        print(f'Error: {input_path} not found')
        sys.exit(1)

    tex = input_path.read_text(encoding='utf-8')
    body = strip_preamble(tex)

    # Strip LaTeX comments
    body = re.sub(r'(?<!\\)%.*$', '', body, flags=re.MULTILINE)

    # Extract tcolorbox (objectives) — may appear anywhere in the document
    tcolorbox = find_env(body, 'tcolorbox')
    objectives_xml = None
    if tcolorbox:
        tcb_start, tcb_end, tcb_inner = tcolorbox
        objectives_xml = build_objectives(tcb_inner)
        body = body[:tcb_start] + body[tcb_end:]

    # Extract title from second {\bf ...} (first is the course label)
    title, body = extract_second_bf(body)

    # Find outer enumerate
    outer = find_env(body, 'enumerate')
    if outer is None:
        print('Error: no \\begin{enumerate} found')
        sys.exit(1)
    outer_start, outer_end, outer_inner = outer

    # Introduction: everything between title removal and outer enumerate
    intro_raw = body[:outer_start].strip()

    # Build exercises from outer enumerate items
    exercises = [build_exercise(item) for item in split_items(outer_inner)]

    # Assemble output
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<worksheet>']

    if title:
        title_xml = convert_text(title)
        parts.append(f'  <title>{title_xml.strip()}</title>')

    if objectives_xml:
        parts.append(objectives_xml)

    if intro_raw and not _is_noise(intro_raw):
        parts.append(build_introduction(intro_raw))

    for ex in exercises:
        parts.append('')
        parts.append(ex)

    parts += ['', '</worksheet>', '']

    output = '\n'.join(parts)

    # Validate XML before writing
    try:
        ET.fromstring(output)
    except ET.ParseError as e:
        print(f'Warning: generated XML failed validation: {e}')

    output_path = input_path.with_suffix('.ptx')
    output_path.write_text(output, encoding='utf-8')
    print(f'Written: {output_path}')


if __name__ == '__main__':
    main()
