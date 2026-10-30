"""PDF extraction worker — uses pymupdf with Tesseract OCR fallback."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_OCR_THRESHOLD = 50


def extract(file_path: str) -> dict:
    """Return {text, pages, method}."""
    import pymupdf
    doc = pymupdf.open(str(file_path))
    pages_text = [page.get_text() for page in doc]
    page_count = len(doc)
    doc.close()

    text = "\n".join(pages_text)

    if len(text.strip()) >= _OCR_THRESHOLD:
        return {"text": text, "pages": page_count, "method": "pymupdf"}

    # OCR fallback
    try:
        import pytesseract
        import pymupdf as fitz

        doc2 = fitz.open(str(file_path))
        ocr_parts = []
        mat = fitz.Matrix(2, 2)
        for page in doc2:
            pix = page.get_pixmap(matrix=mat)
            import PIL.Image, io
            img = PIL.Image.open(io.BytesIO(pix.tobytes("png")))
            ocr_parts.append(pytesseract.image_to_string(img))
        doc2.close()
        ocr_text = "\n".join(ocr_parts)
        return {"text": ocr_text, "pages": page_count, "method": "tesseract-ocr"}
    except ImportError:
        pass
    except Exception as e:
        pass

    return {"text": text, "pages": page_count, "method": "pymupdf-sparse"}
