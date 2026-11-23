import json
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from ...reporting.markdown import generate_markdown
from ...reporting.json_export import generate_json


async def export_report(state, settings):
    if not state['citation_passed'] or not re.fullmatch(r'[a-f0-9]{32}',state['run_id']):
        raise RuntimeError('Report failed the export gate')
    root = settings.output_dir.resolve()
    root.mkdir(parents=True,exist_ok=True)
    destination = root / state['run_id']
    files = {}
    with TemporaryDirectory(dir=root,prefix='.research-') as staging:
        directory = Path(staging) / 'report'
        directory.mkdir()
        for label,filename,renderer in [
            ('markdown','report.md',lambda:generate_markdown(state)),
            ('json','report.json',lambda:json.dumps(generate_json(state),indent=2,ensure_ascii=False)),
        ]:
            if label in settings.formats:
                with (directory / filename).open('w',encoding='utf-8') as handle:
                    handle.write(renderer()+'\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                files[label] = str(destination / filename)
        directory.rename(destination)
    return {'output_files':files}
