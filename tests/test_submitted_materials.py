"""Submitted material inventory preserves original names and attachment lineage."""
import json
import subprocess
import unittest
from audit_core.workbench_runtime import ROOT_HTML


class SubmittedMaterialsTests(unittest.TestCase):
    def test_groups_keep_each_original_and_parent_without_using_technical_paths(self):
        html=ROOT_HTML.read_text('utf-8')
        functions='const submittedMaterialGroups = '+html.split('      const submittedMaterialGroups = ',1)[1].split('      const renderSubmittedMaterials = ',1)[0]
        packet={'archive_id':'a001','source_archive':'D:\\private\\合同资料.zip','inventory':[
            {'file_id':'a001-f1','source_file':'合同/协议.docx','path':'C:/private/rendered.pdf','kind':'visual','suffix':'.docx'},
            {'file_id':'a001-f2','source_file':'返图.xls','suffix':'.xls'},
            {'file_id':'a001-f3','source_file':'返图.xls/图片01.png','suffix':'.png','parent_source':'返图.xls'},
            {'file_id':'a001-f4','source_file':'相同照片.png','suffix':'.png','sha256':'same'},
            {'file_id':'a001-f5','source_file':'另一份照片.png','suffix':'.png','sha256':'same'},
            {'file_id':'a001-f3','source_file':'返图.xls/图片01.png','suffix':'.png','parent_source':'返图.xls'},
            {'file_id':'bad','source_file':''},None,
        ]}
        code=functions+'\nconst packet='+json.dumps(packet,ensure_ascii=True)+';console.log(JSON.stringify([submittedMaterialGroups({a001:packet}),submittedMaterialGroups({archives:[packet]}),submittedMaterialGroups([packet]),submittedMaterialGroups(null),submittedMaterialGroups({unrelated:{source_file:"not-an-inventory"}}),submittedMaterialType(".docx")]));'
        result=subprocess.run(['node','-e',code],capture_output=True,text=True,encoding='utf-8',check=True)
        direct,wrapped,array,missing,unrelated,kind=json.loads(result.stdout)
        self.assertEqual(direct,wrapped);self.assertEqual(direct,array)
        self.assertEqual(direct[0]['archive'],'合同资料.zip')
        self.assertEqual(len(direct[0]['files']),5)
        self.assertEqual(direct[0]['files'][2]['parent'],'返图.xls')
        self.assertEqual(kind,'Word 文档（DOCX）')
        self.assertNotIn('C:/private',json.dumps(direct))
        self.assertEqual(missing,[]);self.assertEqual(unrelated,[])


if __name__=='__main__':unittest.main()
