#    Paperwork - Using OCR to grep dead trees the easy way
#    Copyright (C) 2012-2014  Jerome Flesch
#
#    Paperwork is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Paperwork is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Paperwork.  If not, see <http://www.gnu.org/licenses/>.

import os
import shutil
import logging
import urllib

from gi.repository import GLib
from gi.repository import Gio
from gi.repository import Poppler

from paperwork.backend.common.doc import BasicDoc
from paperwork.backend.pdf.page import PdfPage


PDF_FILENAME = "doc.pdf"
logger = logging.getLogger(__name__)


class PdfDocExporter(object):
    can_select_format = False
    can_change_quality = False

    def __init__(self, doc):
        self.doc = doc
        self.pdfpath = ("%s/%s" % (doc.path, PDF_FILENAME))

    def get_mime_type(self):
        return 'application/pdf'

    def get_file_extensions(self):
        return ['pdf']

    def save(self, target_path):
        shutil.copy(self.pdfpath, target_path)
        return target_path

    def estimate_size(self):
        return os.path.getsize(self.pdfpath)

    def get_img(self):
        return self.doc.pages[0].img

    def __str__(self):
        return 'PDF'


class PdfPagesIterator(object):
    def __init__(self, pdfdoc):
        self.pdfdoc = pdfdoc
        self.idx = 0

    def __iter__(self):
        return self

    def next(self):
        if self.idx >= self.pdfdoc.nb_pages:
            raise StopIteration()
        page = self.pdfdoc.pages[self.idx]
        self.idx += 1
        return page


class PdfPages(object):
    def __init__(self, pdfdoc, pdf):
        self.pdfdoc = pdfdoc
        self.pdf = pdf
        self.page = {}

    def __getitem__(self, idx):
        if isinstance(idx,slice):
            res = []
            for i in range(idx.start or 0, idx.stop or self.pdf.get_n_pages(), idx.step or 1):
                res.append(self[i])
            return res

        if idx < 0:
            idx = self.pdf.get_n_pages() + idx
        if idx not in self.page:
            self.page[idx] = PdfPage(self.pdfdoc, self.pdf, idx)
        return self.page[idx]

    def __len__(self):
        return self.pdf.get_n_pages()

    def __iter__(self):
        return PdfPagesIterator(self.pdfdoc)


class PdfDoc(BasicDoc):
    can_edit = True
    can_split = True
    doctype = u"PDF"
    _pages = None

    def __init__(self, docpath, docid=None):
        BasicDoc.__init__(self, docpath, docid)
        self._pdf = None

    def clone(self):
        return PdfDoc(self.path, self.docid)

    def __get_last_mod(self):
        pdfpath = os.path.join(self.path, PDF_FILENAME)
        last_mod = os.stat(pdfpath).st_mtime
        for page in self.pages:
            if page.last_mod > last_mod:
                last_mod = page.last_mod
        labels_path = os.path.join(self.path, BasicDoc.LABEL_FILE)
        try:
            file_last_mod = os.stat(labels_path).st_mtime
            if file_last_mod > last_mod:
                last_mod = file_last_mod
        except OSError:
            pass
        extra_txt_path = os.path.join(self.path, BasicDoc.EXTRA_TEXT_FILE)
        try:
            file_last_mod = os.stat(extra_txt_path).st_mtime
            if file_last_mod > last_mod:
                last_mod = file_last_mod
        except OSError:
            pass

        return last_mod

    last_mod = property(__get_last_mod)

    def get_pdf_file_path(self):
        return ("%s/%s" % (self.path, PDF_FILENAME))

    def _open_pdf(self):
        if self._pdf:
            return self._pdf
        self._pdf = Poppler.Document.new_from_file(
            ("file://%s/%s" % (urllib.quote(self.path), PDF_FILENAME)),
            password=None)
        return self._pdf

    pdf = property(_open_pdf)

    def __get_pages(self):
        if self._pages is None:
            self._pages = PdfPages(self, self.pdf)
        return self._pages

    pages = property(__get_pages)

    def _get_nb_pages(self):
        if self.is_new:
            # happens when a doc was recently deleted
            return 0
        nb_pages = self.pdf.get_n_pages()
        return nb_pages

    def print_page_cb(self, print_op, print_context, page_nb, keep_refs={}):
        """
        Called for printing operation by Gtk
        """
        self.pages[page_nb].print_page_cb(print_op, print_context,
                                          keep_refs=keep_refs)

    def import_pdf(self, file_uri):
        logger.info("PDF: Importing '%s'" % (file_uri))
        try:
            dest = Gio.File.parse_name("file://%s" % urllib.quote(self.path))
            dest.make_directory(None)
        except GLib.GError, exc:
            logger.exception("Warning: Error while trying to create '%s': %s"
                             % (self.path, exc))
        f = Gio.File.parse_name(file_uri)
        dest = dest.get_child(PDF_FILENAME)
        f.copy(dest,
               0,  # TODO(Jflesch): Missing flags: don't keep attributes
               None, None, None)

    @staticmethod
    def get_export_formats():
        return ['PDF']

    def build_exporter(self, file_format='pdf'):
        return PdfDocExporter(self)

    def drop_cache(self):
        BasicDoc.drop_cache(self)
        if self._pages:
            del self._pages
        self._pages = None
        if self._pdf:
            del self._pdf
        self._pdf = None

    def get_docfilehash(self):
        return BasicDoc.hash_file("%s/%s" % (self.path, PDF_FILENAME))

    def split_pages(self, pages):
        """
        Split the document at these page.
        """

        # You can't leave empty documents
        if 0 in pages:
            pages.remove(0)
        if not pages:
            return

        logger.info("Splitting %s at %s" % (self.docid,pages))

        # Poppler can't work with individual pages, thus we use pdfrw.
        from paperwork.backend.pdf.doc import PDF_FILENAME, PdfDoc
        from paperwork.backend.docimport import SinglePdfImporter
        import pdfrw

        doc_pages = self.pages[:]
        pdir = os.path.abspath(os.path.join(self.path,os.path.pardir))
        new_docs = []

        new_doc = PdfDoc(pdir)
        os.mkdir(new_doc.path)
        new_doc.labels = self.labels[:]

        pdf_r_name = os.path.join(self.path,PDF_FILENAME)
        pdf_a_name = os.path.join(self.path,PDF_FILENAME+'.new')
        pdf_r = pdfrw.PdfReader(pdf_r_name)
        dest = pdfrw.PdfWriter()
        dest_path = pdf_a_name

        offset = 0
        for pdf_page,page in zip(pdf_r.pages,doc_pages):
            if page.page_nb in pages:
                dest.write(dest_path)

                new_doc = PdfDoc(pdir)
                os.mkdir(new_doc.path)
                new_doc.labels = self.labels[:]
                dest = pdf_b = pdfrw.PdfWriter()
                dest_path = os.path.join(new_doc.path,PDF_FILENAME)
                new_docs.append(new_doc)
                offset = page.page_nb
            dest.addpage(pdf_page)
            if offset:
                offset += 1
                page.move_index(new_doc,offset)

        dest.write(dest_path)
        self.drop_cache()

        os.rename(pdf_a_name,pdf_r_name)
        return new_docs

    def destroy_pages(self, pages):
        """
        Delete these pages. May delete the whole document.
        """

        logger.info("Destroying pages: %s %s" % (self,pages))
        if self.nb_pages <= 1:
            self.destroy()
            return

        # Poppler can't delete individual pages, thus we use pdfrw.
        from paperwork.backend.pdf.doc import PDF_FILENAME
        import pdfrw

        doc_pages = self.pages[:]

        pdf_r_name = os.path.join(self.path,PDF_FILENAME)
        pdf_w_name = os.path.join(self.path,PDF_FILENAME+'.new')
        pdf_r = pdfrw.PdfReader(pdf_r_name)
        pdf_w = pdfrw.PdfWriter()

        offset = 0
        for pdf_page,page in zip(pdf_r.pages,doc_pages):
            if page.page_nb in pages:
                pages.remove(page.page_nb)
                for path in (page._box_path, page._thumb_path):
                    if os.access(path, os.F_OK):
                        os.unlink(path)
                offset += 1
            else:
                pdf_w.addpage(pdf_page)
                if offset:
                    page.change_index(offset=-offset)

        pdf_w.write(pdf_w_name)
        os.rename(pdf_w_name,pdf_r_name)
        self.drop_cache()

    def open(self):
        GLib.spawn_async([b"xdg-open",os.path.join(self.path,PDF_FILENAME).encode('utf-8')], flags=GLib.SPAWN_SEARCH_PATH)

def is_pdf_doc(docpath):
    if not os.path.isdir(docpath):
        return False
    try:
        filelist = os.listdir(docpath)
    except OSError, exc:
        logger.exception("Warning: Failed to list files in %s: %s"
                         % (docpath, str(exc)))
        return False
    return PDF_FILENAME in filelist
