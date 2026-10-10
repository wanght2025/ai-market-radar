"""Hash-verified local releases and private recovery; no remote operations."""
import argparse
import ast
import hashlib
import json
import platform
import zipfile
from pathlib import Path, PurePosixPath

from storage import read_json, write_bytes, workspace_lock

ROOT=Path(__file__).resolve().parent
RELEASE_FILES=tuple('akshare_worker.py collector.py dashboard-visuals.js dashboard.css dashboard.html dashboard.js dashboard.py decision.py deepseek_analysis.py enrichment.py history.py market_radar.py opportunities.py participation.py predictions.py refresh_ai_review.py refresh_entry_brief.py reporting.py research_brief.py run.py scheduler.py storage.py release.py version.py config.example.json requirements.txt requirements.lock.txt run.bat install-schedule.ps1 RELEASE.md .gitignore'.split())
RELEASE_FILES+=('research_context.py','research_facts.py','refresh_observations.py','dashboard-research.js','daily_ai.py','update_research.py',
               'README.md','.env.example','DATA_SOURCES.md','VALIDATION.md','ARCHITECTURE.md','RESEARCH_RULES.md','OPERATIONS.md')


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def versions(root):
    values={}
    for filename, names in [('version.py',None),('collector.py',{'SCHEMA'}),('decision.py',{'VERSION'}),
                             ('opportunities.py',{'VERSION'}),('predictions.py',{'REVIEW_VERSION','FORWARD_SELECTION_VERSION'})]:
        for node in ast.parse((root/filename).read_text(encoding='utf-8')).body:
            if isinstance(node,ast.Assign) and isinstance(node.value,ast.Constant):
                for target in node.targets:
                    if isinstance(target,ast.Name) and (names is None or target.id in names):
                        values[filename+':'+target.id]=node.value.value
    return values


def build_release(root, destination, validation=None):
    files=[*RELEASE_FILES, *[str(p.relative_to(root)).replace('\\','/') for pattern in ('prompts/*.txt','tests/*.py','tests/*.js') for p in root.glob(pattern)]]
    missing=runtime_dependencies(root,files)
    if missing:raise ValueError('发行包缺少运行依赖：'+', '.join(sorted(missing)))
    content={name:(root/name).read_bytes() for name in files}
    manifest={'kind':'PUBLIC_RELEASE','versions':versions(root),'python':platform.python_version(),
        'platform':platform.system(),'files':{name:sha256(data) for name,data in content.items()},
        'validation':validation or {'status':'NOT_RECORDED'}}
    destination.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in content.items(): archive.writestr(name,data)
        archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    return manifest


def runtime_dependencies(root, files):
    """Check real local imports, including imports inside functions."""
    public=set(files)
    local={p.stem:p.name for p in root.glob('*.py')}
    missing=set()
    for name in public:
        if '/' in name or not name.endswith('.py'):continue
        for node in ast.walk(ast.parse((root/name).read_text(encoding='utf-8'))):
            names=([r.name for r in node.names] if isinstance(node,ast.Import) else
                   [node.module] if isinstance(node,ast.ImportFrom) and node.module else [])
            for module in names:
                dependency=local.get(module.split('.')[0])
                if dependency and dependency not in public:missing.add(dependency)
    return missing


def verified_contents(bundle, kind):
    with zipfile.ZipFile(bundle) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)):
            raise ValueError('备份/发布包含重复路径，未写入')
        manifest=json.loads(archive.read('manifest.json'))
        if manifest.get('kind')!=kind or set(names)!=set(manifest['files'])|{'manifest.json'}:
            raise ValueError('清单类型或文件集合不匹配，未写入')
        result={}
        for name,expected in manifest['files'].items():
            path=PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
                raise ValueError('路径越界，未写入')
            public=name in RELEASE_FILES or (path.parts[0] in ('prompts','tests') and path.suffix in ('.txt','.py','.js'))
            private=name in ('.env','config.json') or path.parts[0] in ('data','reports')
            if not (public if kind=='PUBLIC_RELEASE' else private):
                raise ValueError('文件不属于允许的发布/恢复范围，未写入')
            data=archive.read(name)
            if sha256(data)!=expected:
                raise ValueError('文件哈希不匹配，未写入')
            result[name]=data
    return result,manifest


def destination_paths(destination, names):
    root=destination.resolve()
    paths={name:destination/name for name in names}
    resolved=[path.resolve() for path in paths.values()]
    if any(not path.is_relative_to(root) for path in resolved):
        raise ValueError('目标路径越界，未写入；请检查目录联接或符号链接')
    if len(set(resolved))!=len(resolved):
        raise ValueError('目标路径重复，未写入')
    return paths


def install_release(bundle, destination):
    content,manifest=verified_contents(bundle,'PUBLIC_RELEASE')
    content['release-manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
    write_recoverable(destination_paths(destination,content),content)
    return manifest


def write_recoverable(paths, content):
    previous={name:path.read_bytes() if path.exists() else None for name,path in paths.items()}
    touched=[]
    try:
        for name,data in content.items():
            path=paths[name]
            path.parent.mkdir(parents=True,exist_ok=True)
            touched.append(name)
            write_bytes(path,data)
    except OSError as error:
        try:
            for name in reversed(touched):
                path=paths[name];data=previous[name]
                if data is None:path.unlink(missing_ok=True)
                elif not path.exists() or path.read_bytes()!=data:write_bytes(path,data)
        except OSError as rollback_error:
            raise OSError(f'写入失败且自动回退未完成；请停止使用并重装或恢复备份。原错误：{error}') from rollback_error
        raise


def backup_private(root,destination):
    files=[p for name in ('.env','config.json') if (p:=root/name).is_file()]
    files += [p for folder in ('data','reports') for p in (root/folder).rglob('*') if p.is_file()
              and p.name!='run.lock' and not p.name.endswith('.tmp')
              and not any(part.startswith('radar_') for part in p.relative_to(root).parts)]
    content={str(p.relative_to(root)).replace('\\','/'):p.read_bytes() for p in files}
    manifest={'kind':'PRIVATE_BACKUP','files':{name:sha256(data) for name,data in content.items()}}
    destination.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in content.items(): archive.writestr(name,data)
        archive.writestr('manifest.json',json.dumps(manifest))
    return manifest


def restore_private(bundle,destination):
    content,manifest=verified_contents(bundle,'PRIVATE_BACKUP')
    write_recoverable(destination_paths(destination,content),content)
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('build','backup','restore','install'))
    parser.add_argument('--file',required=True,type=Path)
    args=parser.parse_args()
    with workspace_lock(ROOT):
        if args.action=='build': result=build_release(ROOT,args.file,read_json(ROOT/'artifacts/release-validation.json',{}))
        elif args.action=='backup': result=backup_private(ROOT,args.file)
        elif args.action=='restore': result=restore_private(args.file,ROOT)
        else: result=install_release(args.file,ROOT)
    print(json.dumps({'kind':result['kind'],'files':len(result['files']),'versions':result.get('versions')},ensure_ascii=True))
