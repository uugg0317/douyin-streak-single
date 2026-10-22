"""Public release text rules; callers select permitted files before opening them."""
import ast
import copy
import hashlib
from pathlib import Path
import re

USER_PATH = re.compile(r'(?:\b[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s"\'<>]+|/(?:home|Users)/[^/\s"\'<>]+)',re.IGNORECASE)
SYNTHETIC_MARKER = 'SYNTHETIC_USER_PATH'
_SECRET_KEYS = r'AUTH_TOKEN|URL_ACCESS_TOKEN|URL_PASSWORD_HASH|URL_PASSWORD_SALT|SMTP_PASS(?:WORD)?|API_KEY|SECRET_KEY|ACCESS_TOKEN|CLIENT_SECRET'
_COOKIE_KEYS = r'sessionid(?:_ss)?|sid_guard|ttwid'
_LITERAL = r'''(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*')'''
_SECRET_ASSIGNMENT = re.compile(
    r'(?<![\w])(?:' + _SECRET_KEYS + r''')["']?\s*\]?\s*[:=]\s*(?P<value>''' + _LITERAL + ')', re.I)
_SECRET_ENV_DEFAULT = re.compile(
    r'''\b(?:getenv|(?:environ\.)?(?:get|setdefault)|_env)\s*\(\s*["'](?:''' + _SECRET_KEYS
    + r''')["']\s*,\s*(?P<value>''' + _LITERAL + ')', re.I)
_BARE_SECRET = re.compile(
    r'(?<![\w])(?:' + _SECRET_KEYS + r')[ \t]*=[ \t]*(?P<value>[^\s\x22\x27`#;<>]+)', re.I)
_COOKIE_HEADER = re.compile(
    r'(?<![\w])(?:' + _COOKIE_KEYS + r')\s*=\s*(?P<value>[^\s;\x22\x27`<>{},]+)', re.I)
_COOKIE_OBJECT = re.compile(r'\{[^{}]*\}', re.S)
_COOKIE_NAME = re.compile(r'''(?:["']name["']|\bname)\s*:\s*["'](?:''' + _COOKIE_KEYS + r''')["']''', re.I)
_COOKIE_VALUE = re.compile(r'''(?:["']value["']|\bvalue)\s*:\s*(?P<value>''' + _LITERAL + ')', re.I)
_AUTHORIZATION = re.compile(r'''\b(?:Bearer|Basic)\s+(?P<value>[A-Za-z0-9._~+/=-]{12,})''', re.I)
_PRIVATE_KEY = re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----')
_SYNTHETIC_VALUE = re.compile(
    r'^(?:synthetic|fixture|unit-test|test|example|placeholder|fake|dummy)(?:$|[-_: ]\S[\s\S]*$)', re.I)
_SYNTHETIC_DECLARATION = re.compile(r'\bSYNTHETIC_SECRET_SHA256\s*=\s*([0-9a-f]{64})\b', re.I)


def _literal_value(raw):
    try:
        value = ast.literal_eval(raw)
        return value if isinstance(value, str) else raw
    except (ValueError, SyntaxError):
        return raw[1:-1]


def _placeholder(value):
    value = value.strip()
    return (not value or bool(_SYNTHETIC_VALUE.fullmatch(value))
            or bool(re.fullmatch(r'<[^<>\r\n]+>', value))
            or bool(re.fullmatch(r'\$\{[A-Za-z_][A-Za-z0-9_]*\}', value))
            or bool(re.fullmatch(r'(?:change[-_]me|replace[-_]me)(?:[-_].*)?', value, re.I))
            or value in {'...', '…'} or value.lower() == '[redacted]'
            or bool(re.fullmatch(r'\*{3,}', value))
            or bool(re.fullmatch(r'(?:scrypt|argon2id|pbkdf2)(?:\$[A-Za-z0-9_=.,-]*)*\$\.\.\.', value)))


def secret_findings(name, raw):
    """Return only line numbers and categories; never return candidate secret values.

    Every text path is checked. Fixed test vectors can declare the SHA-256 of
    that exact value on the same line; this is not a test-directory exemption.
    """
    text = raw.decode('utf-8-sig')
    lines = text.splitlines()
    findings = set()

    def record(offset, kind, value=None):
        number = text.count('\n', 0, offset) + 1
        if value is not None:
            if _placeholder(value):
                return
            if name.startswith('tests/'):
                declared = _SYNTHETIC_DECLARATION.findall(lines[number - 1])
                if hashlib.sha256(value.encode('utf-8')).hexdigest() in declared:
                    return
        findings.add((number, kind))

    for pattern in (_SECRET_ASSIGNMENT, _SECRET_ENV_DEFAULT):
        for match in pattern.finditer(text):
            value = _literal_value(match['value'])
            # A documented print('KEY=' + hash_password(getpass.getpass(...)))
            # contains a closing quote after '='; it is generated input, not a literal.
            if re.fullmatch(r'\s*\+\s*hash_password\(getpass\.getpass\(\s*', value):
                continue
            record(match.start(), 'credential_literal', value)
    # Bare dotenv/shell/document values are not Python/JS variable references.
    suffix = Path(name).suffix.lower()
    if suffix in {'.example', '.md', '.txt', '.sh', '.bat', '.json'}:
        for match in _BARE_SECRET.finditer(text):
            record(match.start(), 'credential_literal', match['value'])
    for match in _COOKIE_HEADER.finditer(text):
        record(match.start(), 'cookie_value', match['value'])
    for container in _COOKIE_OBJECT.finditer(text):
        if _COOKIE_NAME.search(container[0]):
            match = _COOKIE_VALUE.search(container[0])
            if match:
                record(container.start() + match.start(), 'cookie_value', _literal_value(match['value']))
    for match in _AUTHORIZATION.finditer(text):
        record(match.start(), 'authorization_value', match['value'])
    for match in _PRIVATE_KEY.finditer(text):
        record(match.start(), 'private_key')
    return [{'line': line, 'kind': kind} for line, kind in sorted(findings)]


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
