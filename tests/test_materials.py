"""资料自检与负向校验。"""
from pathlib import Path
import json,shutil,tempfile,unittest
from support.check_inputs import check
ROOT=Path(__file__).resolve().parents[1]
class Materials(unittest.TestCase):
    def test_inputs(self):
        r=check(ROOT);self.assertGreater(r['files'],2);self.assertGreater(r['rows'],10)
    def test_corrupt_data(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);shutil.copytree(ROOT/'data',p/'data')
            c=json.loads((p/'data/catalog.json').read_text(encoding='utf-8'))
            f=p/c['files'][0]['path'];f.write_bytes(f.read_bytes()+b' ')
            with self.assertRaisesRegex(AssertionError,'来源指纹不符'):check(p)
    def test_wrong_count(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);shutil.copytree(ROOT/'data',p/'data');f=p/'data/catalog.json'
            c=json.loads(f.read_text(encoding='utf-8'));c['files'][0]['rows']+=1
            f.write_text(json.dumps(c),encoding='utf-8')
            with self.assertRaisesRegex(AssertionError,'来源条数不符'):check(p)
if __name__=='__main__':unittest.main()
