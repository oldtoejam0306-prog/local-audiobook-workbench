import io,zipfile,unittest
from docx import Document
from docx.oxml import OxmlElement
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from unittest.mock import patch
import local_docx_parser as parser


def bytes_of(document):
 out=io.BytesIO();document.save(out);return out.getvalue()

def sample():
 doc=Document();doc.add_paragraph('第一段');table=doc.add_table(rows=2,cols=2)
 table.cell(0,0).text='甲';table.cell(0,1).text='乙';table.cell(1,0).text='丙';table.cell(1,1).text='丁'
 doc.add_paragraph('最后一段');return doc

def rewrite_archive(raw, edits=None, additions=None):
 edits=edits or {};additions=additions or {}
 source=zipfile.ZipFile(io.BytesIO(raw));out=io.BytesIO()
 with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as dst:
  for info in source.infolist():dst.writestr(info,edits.get(info.filename,source.read(info.filename)))
  for name,value in additions.items():dst.writestr(name,value)
 return out.getvalue()

def xml_text_wrapper(tag,text):
 wrapper=OxmlElement(tag)
 if tag=='w:sdt':
  container=OxmlElement('w:sdtContent');wrapper.append(container)
 else:container=wrapper
 paragraph=OxmlElement('w:p');run=OxmlElement('w:r');value=OxmlElement('w:t');value.text=text
 run.append(value);paragraph.append(run);container.append(paragraph)
 return wrapper

class DocxParserTests(unittest.TestCase):
 def test_paragraph_table_interleaving_spans_and_reparse(self):
  raw=bytes_of(sample());result=parser.parse_local_docx_bytes(raw)
  self.assertEqual(result['normalized_text'],'第一段\n甲\t乙\n丙\t丁\n最后一段\n')
  self.assertEqual([x['type'] for x in result['body_spans']],['paragraph','table_row','table_row','paragraph'])
  text=result['normalized_text'];row=result['body_spans'][1]
  self.assertEqual([text[c['start']:c['end']] for c in row['cells']],['甲','乙'])
  self.assertEqual(parser.reparse_and_verify(raw,normalized_text=text,parser_evidence=result['parser_evidence']),result)
  self.assertNotEqual(result['source_sha256'],result['text_sha256']);self.assertEqual(result['parser_evidence']['parser_id'],parser.PARSER_ID)
 def test_cell_paragraphs_and_empty_paragraphs_preserve_boundary(self):
  doc=Document();doc.add_paragraph('开头');doc.add_paragraph('');table=doc.add_table(rows=1,cols=1);cell=table.cell(0,0);cell.text='上';cell.add_paragraph('下');doc.add_paragraph('结束')
  result=parser.parse_local_docx_bytes(bytes_of(doc));self.assertEqual(result['normalized_text'],'开头\n\n上\n下\n结束\n')
  span=result['body_spans'][2]['cells'][0];self.assertEqual(result['normalized_text'][span['start']:span['end']],'上\n下')
 def test_tampered_text_or_evidence_rejected(self):
  raw=bytes_of(sample());r=parser.parse_local_docx_bytes(raw)
  with self.assertRaises(ValueError):parser.reparse_and_verify(raw,normalized_text=r['normalized_text']+'伪',parser_evidence=r['parser_evidence'])
  with self.assertRaises(ValueError):parser.reparse_and_verify(raw,normalized_text=r['normalized_text'],parser_evidence={**r['parser_evidence'],'source_sha256':'0'*64})
 def test_comments_and_notes_and_tracked_changes_rejected(self):
  for tag in ('w:commentRangeStart','w:footnoteReference','w:endnoteReference','w:ins','w:del'):
   with self.subTest(tag=tag):
    doc=sample();doc.paragraphs[0]._p.append(OxmlElement(tag))
    with self.assertRaises(ValueError):parser.parse_local_docx_bytes(bytes_of(doc))
 def test_all_move_customxml_and_property_revision_variants_rejected(self):
  tags=('w:moveFrom','w:moveTo','w:moveFromRangeStart','w:moveFromRangeEnd',
        'w:moveToRangeStart','w:moveToRangeEnd','w:customXmlInsRangeStart','w:customXmlInsRangeEnd',
        'w:customXmlDelRangeStart','w:customXmlDelRangeEnd','w:customXmlMoveFromRangeStart',
        'w:customXmlMoveFromRangeEnd','w:customXmlMoveToRangeStart','w:customXmlMoveToRangeEnd',
        'w:rPrChange','w:pPrChange','w:sectPrChange','w:tblPrChange','w:trPrChange','w:tcPrChange',
        'w:tblGridChange','w:cellIns','w:cellDel','w:cellMerge','w:conflictIns','w:conflictDel')
  for tag in tags:
   with self.subTest(tag=tag):
    doc=Document();doc.add_paragraph('visible');node=OxmlElement(tag)
    if tag in ('w:moveFrom','w:moveTo'):
     run=OxmlElement('w:r');text=OxmlElement('w:t');text.text='revision-only text';run.append(text);node.append(run)
    doc.paragraphs[0]._p.append(node)
    with self.assertRaisesRegex(ValueError,'tracked revision'):parser.parse_local_docx_bytes(bytes_of(doc))
 def test_unknown_or_text_hiding_table_cell_children_rejected(self):
  for tag in ('w:sdt','w:customXml','w:extension'):
   with self.subTest(tag=tag):
    doc=Document();table=doc.add_table(rows=1,cols=1);table.cell(0,0).text='visible cell'
    table.cell(0,0)._tc.append(xml_text_wrapper(tag,'hidden cell text'))
    with self.assertRaises(ValueError):parser.parse_local_docx_bytes(bytes_of(doc))
 def test_unknown_top_level_body_children_rejected(self):
  for tag in ('w:sdt','w:customXml','w:extension'):
   with self.subTest(tag=tag):
    doc=Document();doc.add_paragraph('visible');doc.element.body.append(xml_text_wrapper(tag,'hidden body text'))
    with self.assertRaises(ValueError):parser.parse_local_docx_bytes(bytes_of(doc))
 def test_metadata_properties_cannot_hide_body_text(self):
  doc=Document();paragraph=doc.add_paragraph('visible');ppr=paragraph._p.get_or_add_pPr()
  hidden=OxmlElement('w:p');run=OxmlElement('w:r');text=OxmlElement('w:t');text.text='hidden pPr text';run.append(text);hidden.append(run);ppr.append(hidden)
  with self.assertRaisesRegex(ValueError,'hidden body text'):parser.parse_local_docx_bytes(bytes_of(doc))
  doc=Document();cell=doc.add_table(rows=1,cols=1).cell(0,0);tcpr=cell._tc.get_or_add_tcPr()
  hidden=OxmlElement('w:p');run=OxmlElement('w:r');text=OxmlElement('w:t');text.text='hidden tcPr text';run.append(text);hidden.append(run);tcpr.append(hidden)
  with self.assertRaisesRegex(ValueError,'hidden body text'):parser.parse_local_docx_bytes(bytes_of(doc))
 def test_external_relationships_and_vba_macro_parts_rejected(self):
  doc=Document();doc.add_paragraph('synthetic');doc.part.relate_to('https://example.invalid/linked',RT.HYPERLINK,is_external=True)
  with self.assertRaisesRegex(ValueError,'external relationships'):parser.parse_local_docx_bytes(bytes_of(doc))
  raw=bytes_of(sample())
  with self.assertRaisesRegex(ValueError,'VBA or macro-enabled'):parser.parse_local_docx_bytes(
   rewrite_archive(raw,additions={'word/vbaProject.bin':b'synthetic macro bytes'}))
  edits={
   '[Content_Types].xml':zipfile.ZipFile(io.BytesIO(raw)).read('[Content_Types].xml').replace(
    b'</Types>',b'<Override PartName=\"/word/vbaProject.bin\" ContentType=\"application/vnd.ms-office.vbaProject\"/></Types>'),
   'word/_rels/document.xml.rels':zipfile.ZipFile(io.BytesIO(raw)).read('word/_rels/document.xml.rels').replace(
    b'</Relationships>',b'<Relationship Id=\"rIdVba\" Type=\"http://schemas.microsoft.com/office/2006/relationships/vbaProject\" Target=\"vbaProject.bin\"/></Relationships>'),
  }
  with self.assertRaisesRegex(ValueError,'VBA or macro-enabled'):parser.parse_local_docx_bytes(
   rewrite_archive(raw,edits=edits,additions={'word/vbaProject.bin':b'synthetic macro bytes'}))
 def test_expanded_size_and_member_limits_fail_closed(self):
  raw=bytes_of(sample())
  with patch.object(parser,'_MAX_UNPACKED_BYTES',1):
   with self.assertRaisesRegex(ValueError,'expanded size limit'):parser.parse_local_docx_bytes(raw)
  with patch.object(parser,'_MAX_MEMBERS',1):
   with self.assertRaisesRegex(ValueError,'too many or no parts'):parser.parse_local_docx_bytes(raw)
 def test_nested_and_merged_tables_rejected(self):
  doc=sample();doc.tables[0].cell(0,0).add_table(rows=1,cols=1)
  with self.assertRaisesRegex(ValueError,'nested'):parser.parse_local_docx_bytes(bytes_of(doc))
  doc=sample();doc.tables[0].cell(0,0).merge(doc.tables[0].cell(0,1))
  with self.assertRaisesRegex(ValueError,'merged'):parser.parse_local_docx_bytes(bytes_of(doc))
 def test_bad_archive_and_wrong_name_rejected(self):
  with self.assertRaises(ValueError):parser.parse_local_docx_bytes(b'not zip')
  with self.assertRaises(ValueError):parser.parse_local_docx_bytes(bytes_of(sample()),source_name='book.txt')
  with self.assertRaises(ValueError):parser.parse_local_docx_bytes(bytes_of(sample()),source_name='../book.docx')
 def test_duplicate_zip_part_rejected(self):
  buf=io.BytesIO()
  with zipfile.ZipFile(buf,'w') as z:
   z.writestr('[Content_Types].xml','a');z.writestr('word/document.xml','a');z.writestr('word/document.xml','b')
  with self.assertRaisesRegex(ValueError,'duplicate'):parser.parse_local_docx_bytes(buf.getvalue())

if __name__=='__main__':unittest.main()
