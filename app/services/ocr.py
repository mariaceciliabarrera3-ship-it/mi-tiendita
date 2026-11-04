"""Local, best-effort extraction only. Never changes purchases or stock."""
import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError
from fastapi import HTTPException

Image.MAX_IMAGE_PIXELS = 20_000_000
MAX_BYTES = 8 * 1024 * 1024
MAX_PDF_PIXELS = 20_000_000  # mismo tope que las imágenes, medido a los 150 dpi con que se rasteriza

def run(args, timeout=20):
    try:
        p=subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=True)
        return p.stdout.decode('utf-8','replace')[:100_000]
    except FileNotFoundError:
        raise HTTPException(503, 'OCR no disponible: faltan Tesseract o Poppler en el servidor.')
    except subprocess.TimeoutExpired:
        raise HTTPException(422, 'El documento tardó demasiado. Probá con una foto más simple.')
    except subprocess.CalledProcessError:
        raise HTTPException(422, 'No se pudo leer el documento. Probá con otro archivo o cargá manualmente.')

def number(s):
    s=s.replace('$','').replace(' ','')
    if ',' in s and '.' in s:
        s=s.replace('.','').replace(',','.') if s.rfind(',')>s.rfind('.') else s.replace(',','')
    elif ',' in s: s=s.replace(',','.')
    try:
        v=float(s)
        return v if 0 <= v <= 100000000 else None
    except ValueError: return None

NUM=r'\$?\s*\d[\d.,]*'
# Conservative: name followed by quantity and unit price; optional line total.
PAT=re.compile(r'^\s*(.{2,100}?)\s{2,}('+NUM+r')\s{2,}('+NUM+r')(?:\s{2,}('+NUM+r'))?\s*$')
PAT2=re.compile(r'^\s*(\d+(?:[.,]\d+)?)\s+([A-Za-zÁÉÍÓÚáéíóúÑñ][A-Za-zÁÉÍÓÚáéíóúÑñ0-9 ._/-]{1,80}?)\s{2,}('+NUM+r')(?:\s{2,}('+NUM+r'))?\s*$')

def parse(text):
    rows=[]
    for line in text.splitlines():
        line=line.strip()
        if not line or re.search(r'\b(total|subtotal|iva|cuit|fecha|cantidad|precio|descuento|importe|factura|remito|p[áa]gina)\b',line,re.I): continue
        m2=PAT2.match(line); m=PAT.match(line) if not m2 else None
        if m2: qty,name,price=m2.group(1,2,3)
        elif m: name,qty,price=m.group(1,2,3)
        else: continue
        q=number(qty); p=number(price)
        if q is None or q<=0 or p is None: continue
        rows.append({'description':name.strip(),'quantity':q,'unit_price':p,'source':line[:240]})
        if len(rows)>=60: break
    return rows

def extract(data: bytes):
    if not data or len(data)>MAX_BYTES: raise HTTPException(413,'El archivo debe pesar hasta 8 MB.')
    for cmd in ('tesseract','pdftotext','pdfinfo','pdftoppm'):
        if not shutil.which(cmd): raise HTTPException(503,'OCR no disponible: faltan Tesseract o Poppler en el servidor.')
    with tempfile.TemporaryDirectory(prefix='tiendita-ocr-') as d:
        path=Path(d)
        if data.startswith(b'%PDF-'):
            source=path/'input.pdf';source.write_bytes(data)
            info=run(['pdfinfo','-f','1','-l','3',str(source)])
            pages=re.search(r'^Pages:\s*(\d+)',info,re.M)
            if not pages or int(pages.group(1))>3: raise HTTPException(422,'El PDF debe tener entre 1 y 3 páginas y no estar protegido.')
            # Un PDF liviano puede declarar una página enorme y agotar la memoria al rasterizarla (150 dpi).
            for w,h in re.findall(r'size:\s+([\d.]+) x ([\d.]+) pts',info):
                if (float(w)/72*150)*(float(h)/72*150)>MAX_PDF_PIXELS: raise HTTPException(413,'El PDF tiene una página demasiado grande (máximo equivalente a 20 megapíxeles).')
            text=run(['pdftotext','-layout','-f','1','-l','3',str(source),'-'])
            method='texto PDF'
            if len(re.sub(r'\s','',text))<20:
                run(['pdftoppm','-f','1','-l','3','-r','150','-png',str(source),str(path/'page')],30)
                text='\n'.join(run(['tesseract',str(p),'stdout','-l','spa+eng','--psm','6','-c','preserve_interword_spaces=1'],30) for p in sorted(path.glob('page-*.png')))
                method='OCR PDF escaneado'
        else:
            try:
                img=Image.open(io.BytesIO(data))
                if img.format not in ('JPEG','PNG'): raise HTTPException(415,'Usá JPG, PNG o PDF.')
                if img.width*img.height>20_000_000: raise HTTPException(413,'Imagen demasiado grande (máximo 20 megapíxeles).')
                img=ImageOps.exif_transpose(img).convert('RGB'); img.save(path/'image.png')
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                raise HTTPException(415,'Imagen inválida: usá JPG, PNG o PDF.')
            text=run(['tesseract',str(path/'image.png'),'stdout','-l','spa+eng','--psm','6','-c','preserve_interword_spaces=1'],30);method='OCR imagen'
    return {'method':method,'text':text[:30000],'items':parse(text), 'warning':'Revisá contra el original: cantidades, unidad de medida, precios, impuestos y descuentos pueden estar mal o faltar.'}
