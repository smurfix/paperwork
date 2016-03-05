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

import cairo
import codecs
import os
import logging
import pyocr
import pyocr.builders

from paperwork.backend.common.page import BasicPage
from paperwork.backend.util import split_words
from paperwork.backend.util import surface2image


# By default, PDF are too small for a good image rendering
# so we increase their size
PDF_RENDER_FACTOR = 2
logger = logging.getLogger(__name__)


class PdfWordBox(object):
    def __init__(self, content, rectangle, pdf_size):
        self.content = content
        # XXX(Jflesch): Coordinates seem to come from the bottom left of the
        # page instead of the top left !?
        self.position = ((int(rectangle.x1 * PDF_RENDER_FACTOR),
                          int((pdf_size[1] - rectangle.y2)
                              * PDF_RENDER_FACTOR)),
                         (int(rectangle.x2 * PDF_RENDER_FACTOR),
                          int((pdf_size[1] - rectangle.y1)
                              * PDF_RENDER_FACTOR)))


class PdfLineBox(object):
    def __init__(self, word_boxes, rectangle, pdf_size):
        self.word_boxes = word_boxes
        # XXX(Jflesch): Coordinates seem to come from the bottom left of the
        # page instead of the top left !?
        self.position = ((int(rectangle.x1 * PDF_RENDER_FACTOR),
                          int((pdf_size[1] - rectangle.y2)
                              * PDF_RENDER_FACTOR)),
                         (int(rectangle.x2 * PDF_RENDER_FACTOR),
                          int((pdf_size[1] - rectangle.y1)
                              * PDF_RENDER_FACTOR)))


class PdfPage(BasicPage):
    EXT_TXT = "txt"
    EXT_BOX = "words"

    def __init__(self, doc, pdf, page_nb):
        BasicPage.__init__(self, doc, page_nb)
        self.pdf_page = pdf.get_page(page_nb)
        assert(self.pdf_page is not None)
        size = self.pdf_page.get_size()
        self._size = (int(size[0]), int(size[1]))
        self.__boxes = None
        self.__img_cache = {}

    def get_doc_file_path(self):
        """
        Returns the file path of the image corresponding to this page
        """
        return self.doc.get_pdf_file_path()

    def __get_txt_path(self):
        return self._get_filepath(self.EXT_TXT)

    def __get_box_path(self):
        return self._get_filepath(self.EXT_BOX)

    def __get_last_mod(self):
        try:
            return os.stat(self.__get_box_path()).st_mtime
        except OSError:
            return 0.0

    last_mod = property(__get_last_mod)

    def _get_text(self):
        txtfile = self.__get_txt_path()

        try:
            os.stat(txtfile)

            txt = []
            try:
                with codecs.open(txtfile, 'r', encoding='utf-8') as file_desc:
                    for line in file_desc.readlines():
                        line = line.strip()
                        txt.append(line)
            except IOError, exc:
                logger.error("Unable to read [%s]: %s" % (txtfile, str(exc)))
            return txt

        except OSError, exc:  # os.stat() failed
            pass

        boxfile = self.__get_box_path()
        try:
            os.stat(boxfile)

            # reassemble text based on boxes
            boxes = self.boxes
            txt = []
            for line in boxes:
                txt_line = u""
                for box in line.word_boxes:
                    txt_line += u" " + box.content
                txt.append(txt_line)
            return txt
        except OSError, exc:
            txt = self.pdf_page.get_text()
            txt = unicode(txt, encoding='utf-8')
            return txt.split(u"\n")

    def __get_boxes(self):
        """
        Get all the word boxes of this page.
        """
        if self.__boxes is not None:
            return self.__boxes

        # Check first if there is an OCR file available
        boxfile = self.__get_box_path()
        try:
            os.stat(boxfile)

            box_builder = pyocr.builders.LineBoxBuilder()

            try:
                with codecs.open(boxfile, 'r', encoding='utf-8') as file_desc:
                    self.__boxes = box_builder.read_file(file_desc)
                return self.__boxes
            except IOError, exc:
                logger.error("Unable to get boxes for '%s': %s"
                             % (self.doc.docid, exc))
                # will fall back on pdf boxes
        except OSError, exc:  # os.stat() failed
            pass

        # fall back on what libpoppler tells us

        # TODO: Line support !

        txt = self.pdf_page.get_text()
        pdf_size = self.pdf_page.get_size()
        words = set()
        self.__boxes = []
        for line in txt.split("\n"):
            for word in split_words(unicode(line, encoding='utf-8')):
                words.add(word)
        for word in words:
            for rect in self.pdf_page.find_text(word):
                word_box = PdfWordBox(word, rect, pdf_size)
                line_box = PdfLineBox([word_box], rect, pdf_size)
                self.__boxes.append(line_box)
        return self.__boxes

    def __set_boxes(self, boxes):
        boxfile = self.__get_box_path()
        with codecs.open(boxfile, 'w', encoding='utf-8') as file_desc:
            pyocr.builders.LineBoxBuilder().write_file(file_desc, boxes)
        self.drop_cache()
        self.doc.drop_cache()

    boxes = property(__get_boxes, __set_boxes)

    def __render_img(self, factor):
        # TODO(Jflesch): In a perfect world, we shouldn't use ImageSurface.
        # we should draw directly on the GtkImage.window.cairo_create()
        # context. It would be much more efficient.

        if factor not in self.__img_cache:
            logger.debug('Building img from pdf with factor: %s'
                         % factor)
            width = int(factor * self._size[0])
            height = int(factor * self._size[1])

            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
            ctx = cairo.Context(surface)
            ctx.scale(factor, factor)
            self.pdf_page.render(ctx)
            self.__img_cache[factor] = surface2image(surface)
        return self.__img_cache[factor]

    def __get_img(self):
        return self.__render_img(PDF_RENDER_FACTOR)

    img = property(__get_img)

    def __get_size(self):
        return (self._size[0] * PDF_RENDER_FACTOR,
                self._size[1] * PDF_RENDER_FACTOR)

    size = property(__get_size)

    def split(self):
        """
        Split the document at this page.
        """

        logger.info("Splitting at page: %s" % self)
        if self.page_nb == 0:
            return

        # Poppler can't delete individual pages, thus we use pdfrw.
        from paperwork.backend.pdf.doc import PDF_FILENAME, PdfDoc
        from paperwork.backend.docimport import SinglePdfImporter
        import pdfrw

        doc_pages = self.doc.pages[self.page_nb:]
        pdir = os.path.abspath(os.path.join(self.doc.path,os.path.pardir))

        new_doc = PdfDoc(pdir)
        os.mkdir(new_doc.path)
        new_doc.labels = self.doc.labels[:]

        pdf_r_name = os.path.join(self.doc.path,PDF_FILENAME)
        pdf_a_name = os.path.join(self.doc.path,PDF_FILENAME+'.new')
        pdf_b_name = os.path.join(new_doc.path,PDF_FILENAME)
        pdf_r = pdfrw.PdfReader(pdf_r_name)
        pdf_a = pdfrw.PdfWriter()
        pdf_b = pdfrw.PdfWriter()

        writer = pdf_a
        for cur_page,page in enumerate(pdf_r.pages):
            if cur_page == self.page_nb:
                writer = pdf_b
            writer.addpage(page)
        pdf_a.write(pdf_a_name)
        pdf_b.write(pdf_b_name)

        for i,page in enumerate(doc_pages):
            page.move_index(new_doc,i+1)
        self.doc.drop_cache()

        os.rename(pdf_a_name,pdf_r_name)
        return new_doc

    def destroy(self):
        """
        Delete the page. May delete the whole document if it's actually the
        last page.
        """

        logger.info("Destroying page: %s" % self)
        if self.doc.nb_pages <= 1:
            self.doc.destroy()
            return

        # Poppler can't delete individual pages, thus we use pdfrw.
        from paperwork.backend.pdf.doc import PDF_FILENAME
        import pdfrw

        doc_pages = self.doc.pages[self.page_nb+1:]
        paths = [
            self.__get_box_path(),
            self._get_thumb_path(),
        ]

        pdf_r_name = os.path.join(self.doc.path,PDF_FILENAME)
        pdf_w_name = os.path.join(self.doc.path,PDF_FILENAME+'.new')
        pdf_r = pdfrw.PdfReader(pdf_r_name)
        pdf_w = pdfrw.PdfWriter()

        for cur_page,page in enumerate(pdf_r.pages):
            if cur_page != self.page_nb:
                pdf_w.addpage(page)
        pdf_w.write(pdf_w_name)
        os.rename(pdf_w_name,pdf_r_name)

        for path in paths:
            if os.access(path, os.F_OK):
                os.unlink(path)
        for page in doc_pages:
            page.change_index(offset=-1)
        self.doc.drop_cache()

    def change_index(self, offset=0):
        """
        Move the page number by a given offset. Beware to not let any hole
        in the page numbers when doing this. Make sure also that the wanted
        number is available.
        Will also change the page number of the current object.
        """
        src = {}
        src["box"] = self.__get_box_path()
        src["thumb"] = self._get_thumb_path()

        page_nb = self.page_nb

        page_nb += offset

        logger.info("--> Moving page %d (+%d) to index %d"
                    % (self.page_nb, offset, page_nb))

        self.page_nb = page_nb

        dst = {}
        dst["box"] = self.__get_box_path()
        dst["thumb"] = self._get_thumb_path()

        for key in src.keys():
            if os.access(src[key], os.F_OK):
                if os.access(dst[key], os.F_OK):
                    logger.error("Error: file already exists: %s" % dst[key])
                    assert(0)
                os.rename(src[key], dst[key])

    def move_index(self, new_doc, new_page_nb=1):
        """
        Move the page's index etc. to a new document.
        """
        src = {}
        src["box"] = self.__get_box_path()
        src["thumb"] = self._get_thumb_path()

        page_nb = self.page_nb

        logger.info("--> Moving page %d to %s:%d"
                    % (self.page_nb, new_doc.path, new_page_nb))

        self.drop_cache()
        self.doc = new_doc
        self.page_nb = new_page_nb

        dst = {}
        dst["box"] = self.__get_box_path()
        dst["thumb"] = self._get_thumb_path()

        for key in src.keys():
            if os.access(src[key], os.F_OK):
                if os.access(dst[key], os.F_OK):
                    logger.error("Error: file already exists: %s" % dst[key])
                    assert(0)
                os.rename(src[key], dst[key])

    def print_page_cb(self, print_op, print_context, keep_refs={}):
        ctx = print_context.get_cairo_context()

        logger.debug("Context: %d x %d" % (print_context.get_width(),
                                           print_context.get_height()))
        logger.debug("Size: %d x %d" % (self._size[0], self._size[1]))

        factor_x = float(print_context.get_width()) / float(self._size[0])
        factor_y = float(print_context.get_height()) / float(self._size[1])
        factor = min(factor_x, factor_y)

        logger.debug("Scale: %f x %f --> %f" % (factor_x, factor_y, factor))

        ctx.scale(factor, factor)

        self.pdf_page.render_for_printing(ctx)
        return None
