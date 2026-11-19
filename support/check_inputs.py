"""只检查资料完整性，不计算业务结果。"""
from pathlib import Path
import csv,hashlib,json

def load_rows(path,kind):
    if kind=='json':return json.loads(path.read_text(encoding='utf-8'))
    if kind=='jsonl':return [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines() if x.strip()]
    if kind=='csv':
        with path.open(encoding='utf-8',newline='') as stream:return list(csv.DictReader(stream))
    raise ValueError('来源格式无效')
def check(root):
    root=Path(root).resolve()
    catalog=json.loads((root/'data/catalog.json').read_text(encoding='utf-8'))
    assert catalog['synthetic'] is True,'缺少合成资料声明'
    loaded={}
    for e in catalog['files']:
        path=(root/e['path']).resolve()
        assert path.is_relative_to(root),'来源路径越界'
        assert hashlib.sha256(path.read_bytes()).hexdigest()==e['sha256'],'来源指纹不符'
        rows=load_rows(path,e['format'])
        assert isinstance(rows,list) and len(rows)==e['rows'],'来源条数不符'
        for r in rows:assert isinstance(r,dict) and set(r)==set(e['fields']),'字段不符'
        loaded[e['path']]=rows
    for e in catalog['files']:
        for ref in e['references']:
            target={str(r[ref['target_field']]) for r in loaded[ref['target']]}
            for r in loaded[e['path']]:
                v=r[ref['field']]
                if v is None or v=='':continue
                values=v if isinstance(v,list) else [v]
                assert all(str(x) in target for x in values),'引用不存在'
    return {'files':len(loaded),'rows':sum(map(len,loaded.values())),'scope':'仅资料完整性'}
if __name__=='__main__':print(json.dumps(check(Path(__file__).resolve().parents[1]),ensure_ascii=False))
