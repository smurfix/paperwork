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

import codecs
import datetime
import gettext
import logging
import os.path
import time
import hashlib

from paperwork.backend.labels import Label
from paperwork.backend.util import rm_rf

from gi.repository import GLib

_ = gettext.gettext
logger = logging.getLogger(__name__)


class BasicDoc(object):
    LABEL_FILE = "labels"
    DOCNAME_FORMAT = "%Y%m%d_%H%M_%S"
    EXTRA_TEXT_FILE = "extra.txt"

    pages = []
    can_edit = False
    can_split = False
    _storage = None

    def __init__(self, docpath, docid=None, label_store=None):
        """
        Basic init of common parts of doc.

        Note regarding subclassing: *do not* load the document
        content in __init__(). It would reduce in a huge performance loose
        and thread-safety issues. Load the content on-the-fly when requested.
        """
        assert label_store is not None
        self.label_store = label_store

        if docid is None:
            # new empty doc
            # we must make sure we use an unused id
            basic_docid = time.strftime(self.DOCNAME_FORMAT)
            extra = 0
            docid = basic_docid
            path = os.path.join(docpath, docid)
            while os.access(path, os.F_OK):
                extra += 1
                docid = "%s_%d" % (basic_docid, extra)
                path = os.path.join(docpath, docid)

            self.__docid = docid
            self.path = path
        else:
            self.__docid = docid
            self.path = docpath
        self.__cache = {}

        # We need to keep track of the labels:
        # When updating bayesian filters for label guessing,
        # we need to know the new label list, but also the *previous* label
        # list
        self._previous_labels = self.labels.copy()

    def drop_cache(self):
        self.__cache = {}

    def __str__(self):
        return self.__docid

    def __get_id(self):
        return self.__docid

    id = property(__get_id)

    def __get_last_mod(self):
        raise NotImplementedError()

    last_mod = property(__get_last_mod)

    def __get_nb_pages(self):
        if 'nb_pages' not in self.__cache:
            self.__cache['nb_pages'] = self._get_nb_pages()
        return self.__cache['nb_pages']

    nb_pages = property(__get_nb_pages)

    def print_page_cb(self, print_op, print_context, page_nb, keep_refs={}):
        """
        Arguments:
            keep_refs --- Workaround ugly as fuck to keep some object alive
                          (--> non-garbage-collected) during the whole
                          printing process
        """
        raise NotImplementedError()

    def __get_doctype(self):
        raise NotImplementedError()

    def get_docfilehash(self):
        raise NotImplementedError()

    doctype = property(__get_doctype)

    def __get_keywords(self):
        """
        Yield all the keywords contained in the document.
        """
        for page in self.pages:
            for keyword in page.keywords:
                yield(keyword)

    keywords = property(__get_keywords)

    def destroy(self):
        """
        Delete the document. The *whole* document. There will be no survivors.
        """
        rm_rf(self.path)
        self.drop_cache()

    def add_label(self, label, force=False):
        """
        Add a label to the document.
        """

        logger.info("SetStorageL 1 %s %s %s",self,self._storage,label)
        if label in self.labels and not force:
            return
        logger.info("SetStorageL 2 %s %s %s",self,self._storage,label)
        with codecs.open(os.path.join(self.path, self.LABEL_FILE), 'a',
                         encoding='utf-8') as file_desc:
            name = label.name
            if self._storage is not None and self._storage[0] == label:
                name = "%s::%d" % self._storage
            file_desc.write("%s,%s\n" % (name, label.get_color_str()))

    def remove_label(self, to_remove):
        """
        Remove a label from the document. (-> rewrite the label file)
        """
        if to_remove not in self.labels:
            return
        labels = self.labels
        labels.remove(to_remove)
        with codecs.open(os.path.join(self.path, self.LABEL_FILE), 'w',
                         encoding='utf-8') as file_desc:
            for label in labels:
                name = label.name
                if self._storage is not None and self._storage[0] == label:
                    name = "%s::%d" % self._storage
                file_desc.write("%s,%s\n" % (name, label.get_color_str()))

    def __get_labels(self):
        """
        Read the label file of the documents and extract all the labels

        Returns:
            An array of labels.Label objects
        """
        if 'labels' not in self.__cache:
            labels = set()
            storage = self._storage
            try:
                with codecs.open(os.path.join(self.path, self.LABEL_FILE), 'r',
                                 encoding='utf-8') as file_desc:
                    for line in file_desc.readlines():
                        label_name,label_color = line.strip().split(',',1)
                        if '::' in label_name:
                            label_name,base = label_name.split('::')
                            base = int(base)
                        else:
                            base = None
                        label = Label(name=label_name, color=label_color)
                        if label not in labels:
                            labels.add(label)
                        if base:
                            self._storage = (label,base)
            except IOError:
                pass
            self.__cache['labels'] = labels
            if storage is not None:
                self._storage = storage
        return self.__cache['labels']

    def __set_labels(self, labels):
        """
        Add a label on the document.
        """
        with codecs.open(os.path.join(self.path, self.LABEL_FILE), 'w',
                         encoding='utf-8') as file_desc:
            for label in labels:
                name = label.name
                if self._storage and self._storage[0] == label:
                    name = "%s::%d" % self._storage
                file_desc.write("%s,%s\n" % (name,
                                             label.get_color_str()))
        self.__cache['labels'] = labels

    labels = property(__get_labels, __set_labels)

    def get_index_text(self):
        txt = u""
        for page in self.pages:
            txt += u"\n".join([unicode(line) for line in page.text])
        extra_txt = self.extra_text
        if extra_txt != u"":
            txt += u"\n" + extra_txt + u"\n"
        txt = txt.strip()
        if txt == u"":
            # make sure the text field is not empty. Whoosh doesn't like that
            txt = u"empty"
        return txt

    def _get_text(self):
        txt = u""
        for page in self.pages:
            txt += u"\n".join([unicode(line) for line in page.text])
        extra_txt = self.extra_text
        if extra_txt != u"":
            txt += u"\n" + extra_txt + u"\n"
        txt = txt.strip()
        return txt

    text = property(_get_text)

    def get_index_labels(self):
        return u",".join([unicode(label.name)
                          for label in self.labels])

    def update_label(self, old_label, new_label):
        """
        Update a label

        Will go on each document, and replace 'old_label' by 'new_label'
        """
        logger.info("%s : Updating label ([%s] -> [%s])"
                    % (str(self), old_label.name, new_label.name))
        labels = self.labels
        try:
            labels.remove(old_label)
        except ValueError:
            # this document doesn't have this label
            return

        logger.info("%s : Updating label ([%s] -> [%s])"
                    % (str(self), old_label.name, new_label.name))
        labels.add(new_label)
        with codecs.open(os.path.join(self.path, self.LABEL_FILE), 'w',
                         encoding='utf-8') as file_desc:
            for label in labels:
                name = label.name
                if self._storage is not None and self._storage[0] == label:
                    name = "%s::%d" % self._storage
                file_desc.write("%s,%s\n" % (name, label.get_color_str()))

    @staticmethod
    def get_export_formats():
        raise NotImplementedError()

    def build_exporter(self, file_format='pdf'):
        """
        Returns:
            Returned object must implement the following methods/attributes:
            .can_change_quality = (True|False)
            .set_quality(quality_pourcent)
            .estimate_size() : returns the size in bytes
            .get_img() : returns a Pillow Image
            .get_mime_type()
            .get_file_extensions()
            .save(file_path)
        """
        raise NotImplementedError()

    def __doc_cmp(self, other):
        """
        Comparison function. Can be used to sort docs alphabetically.
        """
        if other is None:
            return -1
        if self.is_new and other.is_new:
            return 0
        id1 = tuple((int(x) for x in self.__docid.split('_')))
        id2 = tuple((int(x) for x in other.__docid.split('_')))
        return cmp(id1,id2)

    def __lt__(self, other):
        return self.__doc_cmp(other) < 0

    def __gt__(self, other):
        return self.__doc_cmp(other) > 0

    def __eq__(self, other):
        return self.__doc_cmp(other) == 0

    def __le__(self, other):
        return self.__doc_cmp(other) <= 0

    def __ge__(self, other):
        return self.__doc_cmp(other) >= 0

    def __ne__(self, other):
        return self.__doc_cmp(other) != 0

    def __hash__(self):
        return hash(self.__docid)

    def __is_new(self):
        if 'new' in self.__cache:
            return self.__cache['new']
        self.__cache['new'] = not os.access(self.path, os.F_OK)
        return self.__cache['new']

    is_new = property(__is_new)

    @staticmethod
    def get_name(date):
        return date.strftime("%x")

    def __get_name(self):
        """
        Returns the localized name of the document (see l10n)
        """
        if self.is_new:
            return _("New document")
        try:
            split = self.__docid.split("_")
            short_docid = "_".join(split[:3])
            datetime_obj = datetime.datetime.strptime(
                short_docid, self.DOCNAME_FORMAT)
            final = datetime_obj.strftime("%x")
            return final
        except Exception, exc:
            logger.error("Unable to parse document id [%s]: %s"
                         % (self.docid, exc))
            return self.docid

    name = property(__get_name)

    def __get_docid(self):
        return self.__docid

    def __set_docid(self, new_base_docid):
        workdir = os.path.dirname(self.path)
        new_docid = new_base_docid
        new_docpath = os.path.join(workdir, new_docid)
        idx = 0

        while os.path.exists(new_docpath):
            idx += 1
            new_docid = new_base_docid + ("_%02d" % idx)
            new_docpath = os.path.join(workdir, new_docid)

        self.__docid = new_docid
        if self.path != new_docpath:
            logger.info("Changing docid: %s -> %s" % (self.path, new_docpath))
            os.rename(self.path, new_docpath)
            self.path = new_docpath

    docid = property(__get_docid, __set_docid)

    def __get_date(self):
        try:
            split = self.__docid.split("_")[0]
            return (datetime.datetime(
                int(split[0:4]),
                int(split[4:6]),
                int(split[6:8])))
        except (IndexError, ValueError):
            return (datetime.datetime(1900, 1, 1))

    def __set_date(self, new_date):
        new_id = ("%02d%02d%02d_0000_01"
                  % (new_date.year,
                     new_date.month,
                     new_date.day))
        self.docid = new_id

    date = property(__get_date, __set_date)

    def __get_extra_text(self):
        extra_txt_file = os.path.join(self.path, self.EXTRA_TEXT_FILE)
        if not os.access(extra_txt_file, os.R_OK):
            return u""
        with codecs.open(extra_txt_file, 'r', encoding='utf-8') as file_desc:
            text = file_desc.read()
            return text

    def __set_extra_text(self, txt):
        extra_txt_file = os.path.join(self.path, self.EXTRA_TEXT_FILE)

        txt = txt.strip()
        if txt == u"":
            os.unlink(extra_txt_file)
        else:
            with codecs.open(extra_txt_file, 'w',
                             encoding='utf-8') as file_desc:
                file_desc.write(txt)

    extra_text = property(__get_extra_text, __set_extra_text)

    @staticmethod
    def hash_file(path):
        dochash = hashlib.sha256(open(path, 'rb').read()).hexdigest()
        return int(dochash, 16)

    def destroy_pages(self, pages):
        raise NotImplementedError()

    def split_pages(self, pages):
        raise NotImplementedError()

    def open(self):
        GLib.spawn_async([b"xdg-open",self.path.encode('utf-8')], flags=GLib.SPAWN_SEARCH_PATH)

    def clone(self):
        return type(self)(self.path, self.docid, label_store=self.label_store)

    def __get_storage(self):
        return self._storage

    def __set_storage(self, label, force=False):
        if not force and self._storage and self._storage[0] == label:
            return
        base = self.label_store.target(label.name, self.nb_pages)
        self._storage = (label, base)
        self.add_label(label, force=True)
        
    def _update_storage(self,pages):
        storage_label = self.storage_label
        if storage_label is None:
            return
        storage_base = self.storage_base
        # If we're at the end of this label's storage, extend
        # if not, reallocate
        if self.label_store.current(storage_label.name) == self.storage_base+self.nb_pages-pages:
            self.label_store.target(storage_label.name,1)
        else:
            self.__set_storage(storage_label, force=True)

    storage = property(__get_storage,__set_storage)

    @property
    def storage_label(self):
        if self._storage is None:
            return None
        return self._storage[0]

    @property
    def storage_name(self):
        if self._storage is None:
            return None
        return self._storage[0].name

    @property
    def storage_base(self):
        if self._storage is None:
            return None
        return self._storage[1]

