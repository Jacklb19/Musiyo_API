"""Reproducible local/EC2 container operations; no cloud provisioning or uploads."""
import argparse
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / 'deployment/compose.demo.yml'


def external_path(value: str) -> Path:
    path = Path(value).resolve()
    if path.is_relative_to(ROOT):
        raise ValueError('Environment files and backups must be outside the repository')
    return path


def compose(environment: Path, *arguments: str, **kwargs):
    result = subprocess.run(['docker', 'compose', '--env-file', str(environment), '-f', str(COMPOSE), *arguments],
                            check=True, **kwargs)
    return result


def checksum(path: Path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def backup(environment: Path, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(timezone.utc).strftime('musiyo-%Y%m%dT%H%M%SZ')
    dump, objects = folder / (stem + '.dump'), folder / (stem + '.objects.tar.gz')
    if dump.exists() or objects.exists():
        raise ValueError('Backup destination already exists')
    # The operator stops writes during the backup window for database/file consistency.
    compose(environment, 'stop', 'api', 'proxy', stdout=subprocess.DEVNULL)
    try:
        with dump.open('wb') as stream:
            compose(environment, 'exec', '-T', 'postgres', 'pg_dump', '-U', 'musiyo', '-d',
                    database_name(environment), '--format=custom', stdout=stream)
        with objects.open('wb') as stream:
            compose(environment, 'run', '--rm', '--no-deps', '--entrypoint', 'tar', 'api',
                    '-C', '/storage', '-czf', '-', '.', stdout=stream)
        manifest = {'schema_version': 1, 'database': database_name(environment),
                    'files': {path.name: checksum(path) for path in (dump, objects)},
                    'object_scope': 'Local delivery volume; remote S3 versions require a separately authorized offline export.'}
        (folder / (stem + '.json')).write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    finally:
        compose(environment, 'start', 'api', 'proxy', stdout=subprocess.DEVNULL)
    print(json.dumps({'backup_manifest': str(folder / (stem + '.json'))}))


def environment_values(environment):
    return dict(line.split('=', 1) for line in environment.read_text(encoding='utf-8').splitlines()
                  if line and not line.startswith('#') and '=' in line)


def database_name(environment):
    database = environment_values(environment).get('MUSIYO_DB_NAME', 'musiyo')
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', database):
        raise ValueError('Invalid database name')
    return database


def restore(environment, manifest_path, target):
    if not re.fullmatch(r'musiyo_restore_[a-z0-9_]{1,40}', target or ''):
        raise ValueError('Restore requires a new musiyo_restore_* database name')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    files = []
    for name, expected in manifest['files'].items():
        path = (manifest_path.parent / name).resolve()
        if path.parent != manifest_path.parent or not path.is_file() or checksum(path) != expected:
            raise ValueError('Backup integrity check failed')
        files.append(path)
    dump = next(path for path in files if path.suffix == '.dump')
    compose(environment, 'exec', '-T', 'postgres', 'createdb', '-U', 'musiyo', target)
    with dump.open('rb') as stream:
        compose(environment, 'exec', '-T', 'postgres', 'pg_restore', '-U', 'musiyo', '-d', target,
                '--no-owner', '--exit-on-error', stdin=stream)
    # Files are inspected in a fresh volume, never merged into live private storage.
    objects = next(path for path in files if path.name.endswith('.objects.tar.gz'))
    with tarfile.open(objects, 'r:gz') as archive:
        for member in archive.getmembers():
            from pathlib import PurePosixPath
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Backup archive contains unsafe paths or special files')
    volume = 'musiyo-demo-' + target + '-private'
    subprocess.run(['docker', 'volume', 'create', volume], check=True, stdout=subprocess.DEVNULL)
    with objects.open('rb') as stream:
        subprocess.run(['docker', 'run', '--rm', '-i', '--user', '0:0', '--entrypoint', 'tar',
                        '-v', volume + ':/restored', environment_values(environment).get('MUSIYO_API_IMAGE', 'musiyo-api:demo'), '-C', '/restored', '-xzf', '-'],
                       check=True, stdin=stream)
    print(json.dumps({'restored_database': target, 'restored_private_volume': volume,
                      'activation': 'Inspect the restored database/files before selecting them. Live configuration is unchanged.'}))


def server_config(folder: Path, quiz_bundle: str | None) -> Path:
    # The container reads these at /config; the templates hold container paths, never keys.
    folder.mkdir(parents=True, exist_ok=False)
    for name in ('guide.json', 'voice.json'):
        shutil.copyfile(ROOT / 'deployment/server-config' / name, folder / name)
    if quiz_bundle:
        shutil.copyfile(external_path(quiz_bundle), folder / 'quiz.json')
    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['create-env', 'migrate', 'transfer', 'start', 'stop', 'health', 'backup', 'restore'])
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--backup-folder')
    parser.add_argument('--backup-manifest')
    parser.add_argument('--restore-database')
    parser.add_argument('--quiz-bundle', help='create-env: published quiz bundle to serve (external file)')
    parser.add_argument('--secrets-dir', help='create-env: external folder with groq.key and elevenlabs.key')
    parser.add_argument('--museum-snapshot', help='transfer: scripts.local_backup folder (museum.db + private/)')
    args = parser.parse_args()
    environment = external_path(args.env_file)
    if args.operation == 'create-env':
        environment.parent.mkdir(parents=True, exist_ok=True)
        config = server_config(environment.parent / 'server-config', args.quiz_bundle)
        secret_folder = external_path(args.secrets_dir) if args.secrets_dir else environment.parent / 'server-secrets'
        secret_folder.mkdir(parents=True, exist_ok=True)
        with environment.open('x', encoding='utf-8') as stream:
            stream.write('MUSIYO_DB_PASSWORD=' + secrets.token_hex(24) + '\nMUSIYO_RESOURCE_SIGNING_KEY=' + secrets.token_hex(32) + '\n')
            stream.write(f'MUSIYO_SERVER_CONFIG_DIR={config.as_posix()}\nMUSIYO_SECRETS_DIR={secret_folder.as_posix()}\n')
            stream.write('MUSIYO_GUIDE_CONFIG_IN_CONTAINER=/config/guide.json\nMUSIYO_VOICE_CONFIG_IN_CONTAINER=/config/voice.json\n')
            if args.quiz_bundle:
                stream.write('MUSIYO_QUIZ_BUNDLE_IN_CONTAINER=/config/quiz.json\n')
        environment.chmod(0o600)
        missing = [name for name in ('groq.key', 'elevenlabs.key') if not (secret_folder / name).is_file()]
        if missing:
            print(f'Put {", ".join(missing)} in {secret_folder} before start (without them the guide quotes fichas and voice is off).')
        return
    if not environment.is_file():
        parser.error('Create or select an existing external environment file first')
    if args.operation in ('start', 'migrate'):
        compose(environment, 'up', '-d', '--build', 'postgres', 'storage-init')
        if args.operation == 'migrate':
            compose(environment, 'run', '--rm', 'api', 'musiyo', 'migrate')
        else:
            compose(environment, 'up', '-d', 'api', 'proxy')
    elif args.operation == 'transfer':
        # Copy a verified local museum snapshot into the empty, migrated deployment database (inside the network).
        if not args.museum_snapshot:
            parser.error('--museum-snapshot is required')
        snapshot = external_path(args.museum_snapshot)
        if not (snapshot / 'museum.db').is_file() or not (snapshot / 'private').is_dir():
            parser.error('The snapshot must hold museum.db and private/ (python -m scripts.local_backup backup)')
        values = environment_values(environment)
        target = f"postgresql+psycopg://musiyo:{values['MUSIYO_DB_PASSWORD']}@postgres:5432/{database_name(environment)}"
        storage = [] if values.get('MUSIYO_STORAGE_BACKEND') == 's3' else ['--target-storage', '/storage']
        compose(environment, 'up', '-d', 'postgres', 'storage-init')
        compose(environment, 'run', '--rm', '-v', f'{snapshot.as_posix()}:/source:ro', 'api', 'python', '-m', 'scripts.transfer_museum',
                '--source-database', '/source/museum.db', '--source-storage', '/source/private',
                '--target-database-url', target, *storage)
    elif args.operation == 'stop':
        compose(environment, 'stop')
    elif args.operation == 'health':
        compose(environment, 'exec', '-T', 'api', 'python', '-c',
                "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/v1/ready').status)")
    elif args.operation == 'backup':
        if not args.backup_folder:
            parser.error('--backup-folder is required')
        backup(environment, external_path(args.backup_folder))
    else:
        if not args.backup_manifest:
            parser.error('--backup-manifest is required')
        restore(environment, external_path(args.backup_manifest), args.restore_database)


if __name__ == '__main__':
    main()
