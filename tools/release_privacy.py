"""Public release text rules; callers select permitted files before opening them."""
import copy
from pathlib import Path
import re

USER_PATH = re.compile(r'(?:\b[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s"\'<>]+|/(?:home|Users)/[^/\s"\'<>]+)',re.IGNORECASE)
SYNTHETIC_MARKER = 'SYNTHETIC_USER_PATH'


def user_path_lines(name, raw):
    text=raw.decode('utf-8-sig')
    return [index for index,line in enumerate(text.splitlines(),1)
            if USER_PATH.search(line) and not (name.startswith('tests/') and SYNTHETIC_MARKER in line)]


def redact_text(text, root):
    for spelling in (str(root),root.as_posix()):
        text=re.sub(re.escape(spelling),lambda match:'<项目根>',text,flags=re.IGNORECASE)
    return USER_PATH.sub('<用户目录>',text)


def public_value(value,root):
    if isinstance(value,str):return redact_text(value,root)
    if isinstance(value,list):return [public_value(item,root) for item in value]
    if isinstance(value,dict):return {key:public_value(item,root) for key,item in value.items()}
    return value


def public_commands(commands,root):
    result=copy.deepcopy(commands)
    for record in result:
        command=record.get('command',[])
        if command:
            executable=Path(command[0]).stem.lower()
            if executable.startswith('python') or executable=='<python>':command[0]='<python>'
            elif executable in {'node','<node>'}:command[0]='<node>'
            record['command']=[redact_text(part,root) for part in command]
    return public_value(result,root)


def evidence_summary(reports):
    lines=['# Test evidence summary','',
           'Raw logs remain with the verifier. SHA-256 and byte counts allow comparison without publishing log contents.','']
    for index,report in enumerate(reports,1):
        lines.extend([f'## Run {index}: '+report['environment'].get('browser_mode','recorded'),'',
            'Commit: `'+report['git_head']+'`',
            'Source SHA-256: `'+report['source_tree_sha256']+'`',
            'Verification: `'+report['verification']+'`',''])
        for record in report['commands']:
            lines.extend(['- Command: `'+' '.join(record['command'])+'`',
                f"  - Passed {record['passed']}; failed {record['failed']}; errors {record['errors']}; skipped {record['skipped']}; skipped_browser {record.get('skipped_browser',0)}.",
                f"  - Evidence: `{record['evidence_log']}`; SHA-256 `{record['sha256']}`; bytes {record['bytes']}."])
            lines.extend('  - Skip: '+reason for reason in record['skip_reasons'])
        lines.append('')
    return ('\n'.join(lines)+'\n').encode('utf-8')
