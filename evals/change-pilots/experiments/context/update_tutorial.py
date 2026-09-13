"""Freeze the upstream tutorial into the Prism agent briefing (no model calls)."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from experiments.build_image import PRISM_COMMIT

HERE = Path(__file__).resolve().parent
MARKER = '<!-- BEGIN PINNED UPSTREAM TUTORIAL -->'
CHAPTERS = ('tutorial.md', *(f'tutorial/{name}.md' for name in (
    'functions', 'data', 'effects', 'continuations', 'coeffects',
    'lenses-streams', 'projects-identity', 'prism-way')))


def render(repository):
    sections = []
    sources = {}
    for name in CHAPTERS:
        raw = subprocess.check_output(['git', '-C', str(repository), 'show',
                                      f'{PRISM_COMMIT}:docs/src/{name}'])
        sources[f'docs/src/{name}'] = hashlib.sha256(raw).hexdigest()
        content = raw.decode()
        # The tutorial documents interpreter Show output; native Show quotes list strings.
        if name == 'tutorial/data.md':
            content = content.replace('[red, green, blue]', '["red", "green", "blue"]')
        # Expand book-only hidden lines; these are scaffolding, not Prism syntax.
        content = re.sub(r'(```prism[^\n]*\n)(.*?)(```)',
            lambda m: m[1] + re.sub(r'^# ?', '', m[2], flags=re.M) + m[3], content, flags=re.S)
        # mdBook tabs are presentation, not instructions or code.
        content = re.sub(r'^\{\{#(?:tabs|tab |endtab|endtabs).*?\}\}\n?', '', content, flags=re.M)
        # Resolve local tutorial links to the same pinned offline documentation.
        def link(m):
            target = m[1]
            if re.match(r'[a-z]+:|/|#', target):
                return m[0]
            import posixpath
            return '](/opt/prism-docs/' + posixpath.normpath(posixpath.join(posixpath.dirname(name), target)) + ')'
        content = re.sub(r'\]\(([^)]+)\)', link, content)
        sections.append(f'<!-- Source: docs/src/{name} -->\n\n' + content.strip())
    return '\n\n---\n\n'.join(sections) + '\n', sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, default=HERE.parents[3])
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    briefing = HERE / 'prism-0.22.md'
    tutorial, sources = render(args.repository)
    prefix = briefing.read_text().split(MARKER)[0].rstrip()
    text = prefix + '\n\n' + MARKER + '\n\n' + tutorial
    manifest = json.dumps({'upstream_commit': PRISM_COMMIT, 'sources_sha256': sources,
                          'tutorial_sha256': hashlib.sha256(tutorial.encode()).hexdigest(),
                          'transformations': ['Expand hidden Prism scaffolding', 'Remove mdBook tab directives',
                                              'Resolve relative links to /opt/prism-docs', 'Use native Show output for the string-list example']}, indent=2) + '\n'
    target = HERE / 'TUTORIAL_SOURCES.json'
    if args.check:
        if briefing.read_text() != text or target.read_text() != manifest:
            raise SystemExit('Frozen tutorial differs from the pinned upstream source')
    else:
        briefing.write_text(text)
        target.write_text(manifest)
    print(f'Prism context: {len(text.split())} words, {len(text.encode())} bytes; {len(sources)} tutorial files')


if __name__ == '__main__':
    main()
