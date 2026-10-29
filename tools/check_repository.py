"""检查 Git 工作区或索引中的公开仓库内容。"""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
REQUIRED={
    'README.md','README.en.md','LICENSE','LICENSING.md','CONTRIBUTING.md',
    'CODE_OF_CONDUCT.md','SECURITY.md','GOVERNANCE.md','CHANGELOG.md','ROADMAP.md',
    'AGENTS.md','.editorconfig','.gitattributes','.gitignore','.github/CODEOWNERS',
    '.github/workflows/docs.yml','.github/PULL_REQUEST_TEMPLATE.md',
    '.github/ISSUE_TEMPLATE/config.yml','.github/ISSUE_TEMPLATE/bug_report.yml',
    '.github/ISSUE_TEMPLATE/feature_request.yml','.github/ISSUE_TEMPLATE/design_question.yml',
    '.github/ISSUE_TEMPLATE/contact_request.yml',
}
PRIVATE_ROOTS={'.git','.cache','.venv','.venv-docs','node_modules','workspaces','captures','diagnostics','logs','output','target','dist','build','coverage'}
BINARY={'.png','.jpg','.jpeg','.webp','.gif','.ico','.zip'}
PATTERNS={
    'GitHub token':re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'API key':re.compile(r'sk-(?:proj-)?[A-Za-z0-9_-]{30,}'),
    'AWS access key':re.compile(r'AKIA[0-9A-Z]{16}'),
    'private key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'personal absolute path':re.compile(r'(?:[A-Za-z]:[\\/]Users[\\/](?!Public(?:[\\/]|$)|Default(?:[\\/]|$))[^\\/\s]+|[A-Za-z]:[\\/]self[\\/])'),
}


def git(*args,input=None):
    result=subprocess.run(['git',*args],cwd=ROOT,input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8',errors='replace').strip())
    return result.stdout


def index_contents():
    rows={}
    for row in git('ls-files','--stage','-z').split(b'\0'):
        if not row:continue
        info,name=row.split(b'\t',1)
        mode,oid,stage=info.decode('ascii').split()
        if stage!='0' or mode not in {'100644','100755'}:
            raise ValueError('索引包含未合并或非普通文件：'+name.decode('utf-8'))
        rows[name.decode('utf-8')]=oid
    ids=list(dict.fromkeys(rows.values()))
    data=git('cat-file','--batch',input=('\n'.join(ids)+'\n').encode('ascii')) if ids else b''
    objects={};offset=0
    for oid in ids:
        end=data.index(b'\n',offset)
        actual,kind,size=data[offset:end].decode('ascii').split()
        if actual!=oid or kind!='blob':raise ValueError('索引对象类型不符')
        offset=end+1;length=int(size);objects[oid]=data[offset:offset+length];offset+=length+1
    return {name:objects[oid] for name,oid in rows.items()}


def working_contents(tracked=False):
    args=['ls-files','-z','--cached']
    if not tracked:args+=['--others','--exclude-standard']
    names={name.decode('utf-8') for name in git(*args).split(b'\0') if name}
    contents={}
    for name in names:
        path=ROOT/name
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError('文件路径越界或为符号链接：'+name)
        if not path.exists():
            continue
        contents[name]=path.read_bytes()
    return contents


def inspect(contents):
    errors=[]
    for name in sorted(REQUIRED-set(contents)):errors.append(name+'：缺少仓库入口')
    for name,data in sorted(contents.items()):
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
            errors.append(name+'：路径不合法')
        if path.parts[0] in PRIVATE_ROOTS or '__pycache__' in path.parts or path.suffix in {'.pyc','.pfx','.p12','.pem','.key'}:
            errors.append(name+'：本地资料或凭据文件')
        if path.name=='.env' or path.name.startswith('.env.') and path.name!='.env.example':
            errors.append(name+'：环境秘密文件')
        if len(data)>100*1024*1024:errors.append(name+'：超过 GitHub 单文件 100MiB 限额')
        if path.suffix.lower() in BINARY:continue
        try:text=data.decode('utf-8')
        except UnicodeDecodeError:
            errors.append(name+'：文本不是 UTF-8');continue
        if data.startswith(b'\xef\xbb\xbf') or b'\r' in data:errors.append(name+'：文本应为 UTF-8 无 BOM 和 LF')
        for number,line in enumerate(text.splitlines(),1):
            for rule,pattern in PATTERNS.items():
                if pattern.search(line):errors.append(f'{name}:{number}：{rule}')
    owners=contents.get('.github/CODEOWNERS',b'').decode('utf-8')
    if not re.fullmatch(r'\* @[A-Za-z0-9][A-Za-z0-9-]*\n',owners):errors.append('.github/CODEOWNERS：维护者规则不完整')
    for name in ('README.md','README.en.md','LICENSING.md','CONTRIBUTING.md'):
        text=contents.get(name,b'').decode('utf-8')
        if 'Apache-2.0' not in text or '(LICENSE)' not in text:errors.append(name+'：许可入口不一致')
    return errors


def main():
    parser=argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--staged',action='store_true',help='读取 Git 索引中的实际 blob')
    group.add_argument('--tracked',action='store_true',help='只检查已跟踪文件')
    args=parser.parse_args()
    try:
        if Path(git('rev-parse','--show-toplevel').decode('utf-8').strip()).resolve()!=ROOT:
            raise ValueError('仓库根目录与项目目录不一致')
        contents=index_contents() if args.staged else working_contents(args.tracked)
        errors=inspect(contents)
    except (RuntimeError,ValueError,OSError) as error:
        print(str(error),file=sys.stderr);return 1
    for error in errors:print(error,file=sys.stderr)
    print(f'仓库内容：{len(contents)} 个文件，{sum(map(len,contents.values()))/1048576:.2f} MiB；许可入口、路径与凭据格式检查；错误 {len(errors)}')
    return 1 if errors else 0


if __name__=='__main__':raise SystemExit(main())
