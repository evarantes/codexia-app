import os
from typing import List, Dict, Optional, Union
from reportlab.lib import colors
from reportlab.lib.pagesizes import A5, A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Image, Table, TableStyle
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT, TA_LEFT

class PDFDocTemplate(SimpleDocTemplate):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.numbering_started = False
        self.has_cover = False

    def afterFlowable(self, flowable):
        """
        Registers TOC entries.
        We look for specific paragraph styles to add to the TOC.
        """
        if flowable.__class__.__name__ == 'Paragraph':
            text = flowable.getPlainText()
            style = flowable.style.name
            
            # Start numbering at Preface, Introduction, or first Chapter
            if style == 'Heading2' and text in ['Prefácio', 'Introdução']:
                self.numbering_started = True
            elif style == 'ChapterTitle':
                self.numbering_started = True

            # Calculate logical page number for TOC
            page_num = self.page
            if self.has_cover:
                page_num -= 1

            # Map styles to TOC levels
            if style == 'ChapterTitle':
                # Level 0
                self.notify('TOCEntry', (0, text, page_num))
            elif style == 'Heading2' and text in ['Introdução', 'Prefácio', 'Posfácio', 'Referências', 'Sobre o Autor', 'Epílogo']:
                # Level 0 for major sections too
                self.notify('TOCEntry', (0, text, page_num))

class BookAssembler:
    """
    Service responsible for assembling the PDF book based on the provided structure.
    Structure follows: External, Pre-textual, Textual, Post-textual.
    """
    
    def __init__(self, output_path: str = "output.pdf"):
        self.output_path = output_path
        self.styles = getSampleStyleSheet()
        self._setup_custom_styles()
        
    def _setup_custom_styles(self):
        """Define custom styles for book formatting"""
        self.styles.add(ParagraphStyle(
            name='BookTitle',
            parent=self.styles['Heading1'],
            fontSize=24,
            leading=28,
            alignment=TA_CENTER,
            spaceAfter=30
        ))
        self.styles.add(ParagraphStyle(
            name='ChapterTitle',
            parent=self.styles['Heading1'],
            fontSize=18,
            leading=22,
            alignment=TA_CENTER,
            spaceBefore=20,
            spaceAfter=20,
            pageBreakBefore=1 # Force new page
        ))
        self.styles.add(ParagraphStyle(
            name='BodyTextJustified',
            parent=self.styles['BodyText'],
            alignment=TA_JUSTIFY,
            leading=14,
            spaceAfter=10,
            firstLineIndent=20
        ))
        self.styles.add(ParagraphStyle(
            name='Dedication',
            parent=self.styles['Italic'],
            alignment=TA_RIGHT,
            fontSize=12,
            leftIndent=150,
            spaceBefore=300
        ))
        self.styles.add(ParagraphStyle(
            name='Epigraph',
            parent=self.styles['Italic'],
            alignment=TA_RIGHT,
            fontSize=11,
            leftIndent=150,
            spaceBefore=200
        ))
        self.styles.add(ParagraphStyle(
            name='Copyright',
            parent=self.styles['Normal'],
            fontSize=8,
            alignment=TA_LEFT,
        ))
        # Style for TOC entries
        self.styles.add(ParagraphStyle(
            name='TOCHeading',
            fontSize=14,
            leading=16,
            spaceAfter=10,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        ))

    def create_book(self, book_data: Dict):
        """
        Main method to generate the book PDF.
        """
        # 1. External Elements (Cover) - Determine early
        cover_path = book_data.get("cover_image")
        has_cover = cover_path and os.path.exists(cover_path)

        # Use custom DocTemplate for TOC support
        doc = PDFDocTemplate(
            self.output_path,
            pagesize=A5,
            rightMargin=2*cm,
            leftMargin=2.5*cm, # Larger inner margin for binding
            topMargin=2*cm,
            bottomMargin=2*cm
        )
        doc.has_cover = has_cover
        
        story = []
        meta = book_data.get("metadata", {})
        sections = book_data.get("sections", {})
        pre = sections.get("pre_textual", {})

        def on_first_page(canvas, doc):
            """Draws the cover image on the first page if available"""
            canvas.saveState()
            if has_cover:
                # Draw cover full page (ignore margins)
                page_width, page_height = doc.pagesize
                # Use preserveAspectRatio=False to force fill (might stretch)
                # or True to fit within. User requested "full page", so we fill.
                canvas.drawImage(cover_path, 0, 0, width=page_width, height=page_height, preserveAspectRatio=False, mask='auto')
            canvas.restoreState()

        def on_later_pages(canvas, doc):
            """Draws page numbers starting from Preface"""
            # Calculate logical page number
            page_num = doc.page
            if has_cover:
                page_num -= 1
            
            if getattr(doc, 'numbering_started', False):
                canvas.saveState()
                canvas.setFont('Helvetica', 10)
                # Draw page number centered at the bottom
                page_width, _ = doc.pagesize
                canvas.drawCentredString(page_width / 2, 0.75 * cm, str(page_num))
                canvas.restoreState()

        if has_cover:
            # Add a PageBreak to ensure the first page is devoted to the cover
            # The content of the first page is drawn by on_first_page
            story.append(PageBreak())

        # 2. Pre-textual Elements
        
        # Falsa Folha de Rosto (Half Title)
        if pre.get("false_title"):
            story.append(Spacer(1, 100))
            story.append(Paragraph(meta.get("title", ""), self.styles['Title']))
            story.append(PageBreak())
            
        # Folha de Rosto (Title Page)
        if pre.get("title_page"):
            story.append(Spacer(1, 100))
            story.append(Paragraph(meta.get("title", ""), self.styles['BookTitle']))
            if meta.get("subtitle"):
                story.append(Paragraph(meta.get("subtitle", ""), self.styles['Heading2']))
            story.append(Spacer(1, 50))
            story.append(Paragraph(meta.get("author", ""), self.styles['Normal']))
            story.append(Spacer(1, 150))
            story.append(Paragraph("Edição do Autor", self.styles['Normal']))
            story.append(Paragraph("2025", self.styles['Normal']))
            story.append(PageBreak())
            
        # Copyright Page
        if pre.get("copyright"):
            story.append(Spacer(1, 400)) # Push to bottom
            copy_text = pre.get("copyright", {}).get("text", f"Copyright © 2025 {meta.get('author')}")
            story.append(Paragraph("<b>Ficha Catalográfica</b>", self.styles['Normal']))
            story.append(Paragraph(copy_text, self.styles['Copyright']))
            story.append(PageBreak())
            
        # Dedication
        if pre.get("dedication"):
            story.append(Paragraph(pre.get("dedication"), self.styles['Dedication']))
            story.append(PageBreak())
            
        # Acknowledgments
        if pre.get("acknowledgments"):
            story.append(Paragraph("Agradecimentos", self.styles['Heading2']))
            story.append(Paragraph(pre.get("acknowledgments"), self.styles['BodyTextJustified']))
            story.append(PageBreak())
            
        # Epigraph
        if pre.get("epigraph"):
            story.append(Paragraph(pre.get("epigraph"), self.styles['Epigraph']))
            story.append(PageBreak())
            
        # Preface
        if pre.get("preface"):
            story.append(Paragraph("Prefácio", self.styles['Heading2']))
            for para in pre.get("preface", "").split('\n'):
                if para.strip():
                    story.append(Paragraph(para, self.styles['BodyTextJustified']))
            story.append(PageBreak())

        # Table of Contents (Automated)
        story.append(Paragraph("Sumário", self.styles['Heading2']))
        toc = TableOfContents()
        toc.levelStyles = [
            ParagraphStyle(fontName='Helvetica', fontSize=10, name='TOCHeading1', leftIndent=20, firstLineIndent=-20, spaceBefore=5, leading=12),
            ParagraphStyle(fontName='Helvetica', fontSize=10, name='TOCHeading2', leftIndent=40, firstLineIndent=-20, spaceBefore=0, leading=12),
        ]
        story.append(toc)
        # story.append(PageBreak()) # Removed to avoid blank page before Introduction

        # 3. Textual Elements (Body)
        if pre.get("introduction"):
             story.append(Paragraph("Introdução", self.styles['ChapterTitle']))
             for para in pre.get("introduction", "").split('\n'):
                 if para.strip():
                     story.append(Paragraph(para, self.styles['BodyTextJustified']))
             # story.append(PageBreak()) # Removed to avoid blank page before Chapter 1

        chapters = sections.get("textual", [])
        for chapter in chapters:
            title = chapter.get("title", "")
            content = chapter.get("content", "")
            
            # Note: ChapterTitle style has pageBreakBefore=1, so it ensures new page.
            story.append(Paragraph(title, self.styles['ChapterTitle']))
            
            # Split by newlines to create paragraphs
            paragraphs = content.split('\n')
            for p_text in paragraphs:
                if p_text.strip():
                    story.append(Paragraph(p_text, self.styles['BodyTextJustified']))
            
            # story.append(PageBreak()) # Removed to avoid blank page between chapters

        # 4. Post-textual Elements
        post = sections.get("post_textual", {})
        
        if post.get("epilogue"):
            story.append(Paragraph("Epílogo", self.styles['ChapterTitle']))
            story.append(Paragraph(post.get("epilogue"), self.styles['BodyTextJustified']))
            # story.append(PageBreak()) # Removed to avoid blank page before About Author
            
        if post.get("about_author"):
            # Use ChapterTitle to ensure page break before it
            story.append(Paragraph("Sobre o Autor", self.styles['ChapterTitle']))
            story.append(Paragraph(post.get("about_author"), self.styles['BodyTextJustified']))
            story.append(PageBreak())

        # Build with multiBuild to resolve TOC
        doc.multiBuild(story, onFirstPage=on_first_page, onLaterPages=on_later_pages)
        return self.output_path
