"""Put the optimized content into the source document.

The output is the source file's object graph (catalog, layers, metadata, georeferencing, fonts,
images, page dictionary) with only the page content stream replaced and the new chunk Form
XObjects (/Ck*) added to the page's XObject resources.

usage: stage_assemble.py source.pdf chunked.pdf out.pdf
"""
import sys
import zlib

import pikepdf


def main():
    src, opt, out = sys.argv[1:4]
    o = pikepdf.open(opt)
    pdf = pikepdf.open(src)
    page, opage = pdf.pages[0], o.pages[0]
    xo = page.Resources.XObject
    new_names = [k for k in opage.Resources.XObject.keys() if k.startswith('/Ck')]
    clash = [k for k in new_names if k in xo]
    assert not clash, f'source already uses XObject names {clash[:5]}'
    # copy all chunk forms in one graph copy so shared sub-objects stay shared
    holder = o.make_indirect(pikepdf.Dictionary({k: opage.Resources.XObject[k] for k in new_names}))
    copied = pdf.copy_foreign(holder)
    for k in new_names:
        xo[k] = copied[k]
    s = pikepdf.Stream(pdf, zlib.compress(opage.Contents.read_bytes(), 9))
    s.Filter = pikepdf.Name.FlateDecode
    page.obj.Contents = pdf.make_indirect(s)
    pdf.save(out, compress_streams=True, recompress_flate=False, fix_metadata_version=False,
             object_stream_mode=pikepdf.ObjectStreamMode.generate, deterministic_id=True)
    print(f'assemble: {len(new_names)} forms added to the source document')


if __name__ == '__main__':
    main()
