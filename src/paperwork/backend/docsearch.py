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
"""
Contains all the code relative to keyword and document list management list.
Also everything related to indexation and searching in the documents (+
suggestions)
"""

import logging
import copy
import datetime
import os.path

from gi.repository import GObject

import whoosh.fields
import whoosh.index
import whoosh.qparser
import whoosh.query
import whoosh.sorting

from paperwork.backend.common.page import BasicPage
from paperwork.backend.img.doc import ImgDoc
from paperwork.backend.img.doc import is_img_doc
from paperwork.backend.labels import LabelGuesser
from paperwork.backend.pdf.doc import PdfDoc
from paperwork.backend.pdf.doc import is_pdf_doc
from paperwork.backend.util import dummy_progress_cb
from paperwork.backend.util import MIN_KEYWORD_LEN
from paperwork.backend.util import mkdir_p
from paperwork.backend.util import rm_rf
from paperwork.backend.util import strip_accents


logger = logging.getLogger(__name__)

DOC_TYPE_LIST = [
    (is_pdf_doc, PdfDoc.doctype, PdfDoc),
    (is_img_doc, ImgDoc.doctype, ImgDoc)
]


class DummyDocSearch(object):
    """
    Dummy doc search object.

    Instantiating a DocSearch object takes time (the time to rereard the
    index). So you can use this object instead during this time as a
    placeholder
    """
    docs = []
    label_list = []

    def __init__(self):
        pass

    @staticmethod
    def get_doc_examiner():
        """ Do nothing """
        assert()

    @staticmethod
    def get_index_updater():
        """ Do nothing """
        assert()

    @staticmethod
    def find_suggestions(*args, **kwargs):
        """ Do nothing """
        return []

    @staticmethod
    def find_documents(*args, **kwargs):
        """ Do nothing """
        return []

    @staticmethod
    def create_label(*args, **kwargs):
        """ Do nothing """
        assert()

    @staticmethod
    def add_label(*args, **kwargs):
        """ Do nothing """
        assert()

    @staticmethod
    def remove_label(*args, **kwargs):
        """ Do nothing """
        assert()

    @staticmethod
    def update_label(*args, **kwargs):
        """ Do nothing """
        assert()

    @staticmethod
    def destroy_label(*args, **kwargs):
        """ Do nothing """
        assert()

    @staticmethod
    def destroy_index():
        """ Do nothing """
        assert()

    @staticmethod
    def is_hash_in_index(*args, **kwargs):
        """ Do nothing """
        assert()

    @staticmethod
    def guess_labels(*args, **kwargs):
        """ Do nothing """
        assert()

    @staticmethod
    def get(*args, **kwargs):
        """ Do nothing """
        return None

    @staticmethod
    def get_doc_from_docid(docid, doc_type_name=None, inst=False):
        """ Do nothing """
        return None


class DocDirExaminer(GObject.GObject):
    """
    Examine a directory containing documents. It looks for new documents,
    modified documents, or deleted documents.
    """
    def __init__(self, docsearch):
        GObject.GObject.__init__(self)
        self.docsearch = docsearch
        # we may be run in an independent thread --> use an independent
        # searcher
        self.__searcher = docsearch.index.searcher()

    def examine_rootdir(self,
                        on_new_doc,
                        on_doc_modified,
                        on_doc_deleted,
                        on_doc_unchanged,
                        progress_cb=dummy_progress_cb):
        """
        Examine the rootdir.
        Calls on_new_doc(doc), on_doc_modified(doc), on_doc_deleted(docid)
        every time a new, modified, or deleted document is found
        """
        # getting the doc list from the index
        query = whoosh.query.Every()
        results = self.__searcher.search(query, limit=None)
        old_doc_list = [result['docid'] for result in results]
        old_doc_infos = {}
        for result in results:
            old_doc_infos[result['docid']] = (result['doctype'],
                                              result['last_read'])
        old_doc_list = set(old_doc_list)

        # and compare it to the current directory content
        docdirs = os.listdir(self.docsearch.rootdir)
        progress = 0
        for docdir in docdirs:
            old_infos = old_doc_infos.get(docdir)
            doctype = None
            if old_infos is not None:
                doctype = old_infos[0]
            doc = self.docsearch.get_doc_from_docid(docdir, doctype, inst=True)
            if doc is None:
                continue
            if docdir in old_doc_list:
                old_doc_list.remove(docdir)
                assert(old_infos is not None)
                last_mod = datetime.datetime.fromtimestamp(doc.last_mod)
                if old_infos[1] != last_mod:
                    on_doc_modified(doc)
                else:
                    on_doc_unchanged(doc)
            else:
                on_new_doc(doc)
            progress_cb(progress, len(docdirs),
                        DocSearch.INDEX_STEP_CHECKING, doc)
            progress += 1

        # remove all documents from the index that don't exist anymore
        for old_doc in old_doc_list:
            # Will be a document with 0 pages
            docpath = os.path.join(self.docsearch.rootdir, old_doc)
            on_doc_deleted(ImgDoc(docpath, old_doc))

        progress_cb(1, 1, DocSearch.INDEX_STEP_CHECKING)


class DocIndexUpdater(GObject.GObject):
    """
    Update the index content.
    Don't forget to call commit() to apply the changes
    """
    def __init__(self, docsearch, optimize, progress_cb=dummy_progress_cb):
        self.docsearch = docsearch
        self.optimize = optimize
        self.index_writer = docsearch.index.writer()
        self.label_guesser_updater = docsearch.label_guesser.get_updater()
        self.progress_cb = progress_cb

    def _update_doc_in_index(self, index_writer, doc):
        """
        Add/Update a document in the index
        """
        all_labels = set(self.docsearch.label_list)
        doc_labels = set(doc.labels)
        new_labels = doc_labels.difference(all_labels)

        # can happen when we recreate the index from scract
        for label in new_labels:
            self.docsearch.create_label(label)

        last_mod = datetime.datetime.fromtimestamp(doc.last_mod)
        docid = unicode(doc.docid)

        dochash = doc.get_docfilehash()
        dochash = (u"%X" % dochash)

        doc_txt = doc.get_index_text()
        assert(isinstance(doc_txt, unicode))
        labels_txt = doc.get_index_labels()
        assert(isinstance(labels_txt, unicode))

        query = whoosh.query.Term("docid", docid)
        index_writer.delete_by_query(query)

        index_writer.update_document(
            docid=docid,
            doctype=doc.doctype,
            docfilehash=dochash,
            content=strip_accents(doc.get_index_text()),
            label=strip_accents(doc.get_index_labels()),
            date=doc.date,
            last_read=last_mod
        )
        return True

    @staticmethod
    def _delete_doc_from_index(index_writer, docid):
        """
        Remove a document from the index
        """
        query = whoosh.query.Term("docid", docid)
        index_writer.delete_by_query(query)

    def add_doc(self, doc):
        """
        Add a document to the index
        """
        logger.info("Indexing new doc: %s" % doc)
        self._update_doc_in_index(self.index_writer, doc)
        self.label_guesser_updater.add_doc(doc)
        if doc.docid not in self.docsearch._docs_by_id:
            self.docsearch._docs_by_id[doc.docid] = doc

    def upd_doc(self, doc):
        """
        Update a document in the index
        """
        logger.info("Updating modified doc: %s" % doc)
        self._update_doc_in_index(self.index_writer, doc)
        self.label_guesser_updater.upd_doc(doc)

    def del_doc(self, doc):
        """
        Delete a document
        """
        logger.info("Removing doc from the index: %s" % doc)
        if doc.docid in self.docsearch._docs_by_id:
            self.docsearch._docs_by_id.pop(doc.docid)
        if isinstance(doc, str) or isinstance(doc, unicode):
            # annoying case : we can't know which labels were on it
            # so we can't roll back the label guesser training ...
            self._delete_doc_from_index(self.index_writer, doc)
            return
        self._delete_doc_from_index(self.index_writer, doc.docid)
        self.label_guesser_updater.del_doc(doc)

    def commit(self):
        """
        Apply the changes to the index
        """
        logger.info("Index: Commiting changes")
        self.index_writer.commit()
        del self.index_writer
        self.label_guesser_updater.commit()

        self.docsearch.reload_searcher()

    def cancel(self):
        """
        Forget about the changes
        """
        logger.info("Index: Index update cancelled")
        self.index_writer.cancel()
        del self.index_writer
        self.label_guesser_updater.cancel()


class DocSearch(object):
    """
    Index a set of documents. Can provide:
        * documents that match a list of keywords
        * suggestions for user input.
        * instances of documents
    """

    INDEX_STEP_LOADING = "loading"
    INDEX_STEP_CHECKING = "checking"
    INDEX_STEP_READING = "checking"
    INDEX_STEP_COMMIT = "commit"
    LABEL_STEP_UPDATING = "label updating"
    LABEL_STEP_DESTROYING = "label deletion"
    WHOOSH_SCHEMA = whoosh.fields.Schema(
        # static up to date schema
        docid=whoosh.fields.ID(stored=True, unique=True),
        doctype=whoosh.fields.ID(stored=True, unique=False),
        docfilehash=whoosh.fields.ID(stored=True),
        content=whoosh.fields.TEXT(spelling=True),
        label=whoosh.fields.KEYWORD(stored=True, commas=True,
                                    scorable=True),
        date=whoosh.fields.DATETIME(stored=True),
        last_read=whoosh.fields.DATETIME(stored=True),
    )

    def __init__(self, rootdir, indexdir=None,
                 callback=dummy_progress_cb, label_store=None):
        """
        Index files in rootdir (see constructor)

        Arguments:
            callback --- called during the indexation (may be called *often*).
                step : DocSearch.INDEX_STEP_READING or
                    DocSearch.INDEX_STEP_SORTING
                progression : how many elements done yet
                total : number of elements to do
                document (only if step == DocSearch.INDEX_STEP_READING): file
                    being read
        """
        assert(label_store)
        self.label_store = label_store
        self.rootdir = rootdir
        if indexdir is None:
            base_data_dir = os.getenv(
                "XDG_DATA_HOME",
                os.path.expanduser("~/.local/share")
            )
            indexdir = os.path.join(base_data_dir, "paperwork")
        self.indexdir = os.path.join(indexdir, "index")
        mkdir_p(self.indexdir)
        self.label_guesser_dir = os.path.join(indexdir, "label_guessing")
        mkdir_p(self.label_guesser_dir)

        self._docs_by_id = {}  # docid --> doc
        self.labels = {}  # label name --> label

        need_index_rewrite = True
        try:
            logger.info("Opening index dir '%s' ..." % self.indexdir)
            self.index = whoosh.index.open_dir(self.indexdir)
            # check that the schema is up-to-date
            # We use the string representation of the schemas, because previous
            # versions of whoosh don't always implement __eq__
            if str(self.index.schema) == str(self.WHOOSH_SCHEMA):
                need_index_rewrite = False
        except (whoosh.index.EmptyIndexError, ValueError) as exc:
            logger.warning("Failed to open index '%s'" % self.indexdir)
            logger.warning("Exception was: %s" % str(exc))

        if need_index_rewrite:
            logger.info("Creating a new index")
            self.index = whoosh.index.create_in(self.indexdir,
                                                self.WHOOSH_SCHEMA)
            logger.info("Index '%s' created" % self.indexdir)

        self.__searcher = self.index.searcher()

        class CustomFuzzy(whoosh.qparser.query.FuzzyTerm):
            def __init__(self, fieldname, text, boost=1.0, maxdist=1,
                         prefixlength=0, constantscore=True):
                whoosh.qparser.query.FuzzyTerm.__init__(
                    self, fieldname, text, boost, maxdist,
                    prefixlength, constantscore=True
                )

        facets = [
            whoosh.sorting.ScoreFacet(),
            whoosh.sorting.FieldFacet("date", reverse=True)
        ]

        self.search_param_list = {
            'fuzzy': [
                {
                    "query_parser": whoosh.qparser.MultifieldParser(
                        ["label", "content"], schema=self.index.schema,
                        termclass=CustomFuzzy),
                    "sortedby": facets
                },
                {
                    "query_parser": whoosh.qparser.MultifieldParser(
                        ["label", "content"], schema=self.index.schema,
                        termclass=whoosh.qparser.query.Prefix),
                    "sortedby": facets
                },
            ],
            'strict': [
                {
                    "query_parser": whoosh.qparser.MultifieldParser(
                        ["label", "content"], schema=self.index.schema,
                        termclass=whoosh.query.Term),
                    "sortedby": facets
                },
            ],
        }

        self.label_guesser = LabelGuesser(self.label_guesser_dir)

        self.check_workdir()
        self.reload_index(callback)

    def check_workdir(self):
        """
        Check that the current work dir (see config.PaperworkConfig) exists. If
        not, open the settings dialog.
        """
        mkdir_p(self.rootdir)

    def get_doc_examiner(self):
        """
        Return an object useful to find added/modified/removed documents
        """
        return DocDirExaminer(self)

    def get_index_updater(self, optimize=True):
        """
        Return an object useful to update the content of the index

        Note that this object is only about modifying the index. It is not
        made to modify the documents themselves.
        Some helper methods, with more specific goals, may be available for
        what you want to do.
        """
        return DocIndexUpdater(self, optimize)

    def guess_labels(self, doc):
        """
        return a prediction of label names
        """
        if doc.nb_pages <= 0:
            return []
        label_names = self.label_guesser.guess(doc)
        labels = set()
        for label_name in label_names:
            label = self.labels[label_name]
            labels.add(label)
        return labels

    def __inst_doc(self, docid, doc_type_name=None):
        """
        Instantiate a document based on its document id.
        The information are taken from the whoosh index.
        """
        doc = None
        docpath = os.path.join(self.rootdir, docid)
        if not os.path.exists(docpath):
            return None
        if doc_type_name is not None:
            # if we already know the doc type name
            for (is_doc_type, doc_type_name_b, doc_type) in DOC_TYPE_LIST:
                if doc_type_name_b == doc_type_name:
                    doc = doc_type(docpath, docid, label_store=self.label_store)
            if not doc:
                logger.warning(
                    ("Warning: unknown doc type found in the index: %s") %
                    doc_type_name
                )
        # otherwise we guess the doc type
        if not doc:
            for (is_doc_type, doc_type_name, doc_type) in DOC_TYPE_LIST:
                if is_doc_type(docpath):
                    doc = doc_type(docpath, docid, label_store=self.label_store)
                    break
        if not doc:
            logger.warning("Warning: unknown doc type for doc '%s'" % docid)

        return doc

    def get_doc_from_docid(self, docid, doc_type_name=None, inst=False):
        """
        Try to find a document based on its document id. If it hasn't been
        instantiated yet, it will be.
        """
        assert(docid is not None)
        if docid in self._docs_by_id:
            return self._docs_by_id[docid]
        if not inst:
            return None
        doc = self.__inst_doc(docid, doc_type_name)
        if doc is None:
            return None
        self._docs_by_id[docid] = doc
        return doc

    def reload_index(self, progress_cb=dummy_progress_cb):
        """
        Read the index, and load the document list from it
        """
        docs_by_id = self._docs_by_id
        self._docs_by_id = {}
        for doc in docs_by_id.values():
            doc.drop_cache()
        del docs_by_id

        query = whoosh.query.Every()
        results = self.__searcher.search(query, limit=None)

        nb_results = len(results)
        progress = 0
        labels = set()

        for result in results:
            docid = result['docid']
            doctype = result['doctype']
            doc = self.__inst_doc(docid, doctype)
            if doc is None:
                continue
            progress_cb(progress, nb_results, self.INDEX_STEP_LOADING, doc)
            self._docs_by_id[docid] = doc
            for label in doc.labels:
                labels.add(label)

            progress += 1
        progress_cb(1, 1, self.INDEX_STEP_LOADING)

        self.label_guesser = LabelGuesser(self.label_guesser_dir)
        for label in labels:
            self.label_guesser.load(label.name)

        self.labels = {label.name: label for label in labels}

    def index_page(self, page):
        """
        Extract all the keywords from the given page

        Arguments:
            page --- from which keywords must be extracted

        Obsolete. To remove. Use get_index_updater() instead
        """
        updater = self.get_index_updater(optimize=False)
        updater.upd_doc(page.doc)
        updater.commit()
        if page.doc.docid not in self._docs_by_id:
            logger.info("Adding document '%s' to the index" % page.doc.docid)
            assert(page.doc is not None)
            self._docs_by_id[page.doc.docid] = page.doc

    def __get_all_docs(self):
        """
        Return all the documents. Beware, they are unsorted.
        """
        return self._docs_by_id.values()

    docs = property(__get_all_docs)

    def get(self, obj_id):
        """
        Get a document or a page using its ID
        Won't instantiate them if they are not yet available
        """
        if BasicPage.PAGE_ID_SEPARATOR in obj_id:
            (docid, page_nb) = obj_id.split(BasicPage.PAGE_ID_SEPARATOR)
            page_nb = int(page_nb)
            return self._docs_by_id[docid].pages[page_nb]
        return self._docs_by_id[obj_id]

    def find_documents(self, sentence, limit=None, must_sort=True,
                       search_type='fuzzy'):
        """
        Returns all the documents matching the given keywords

        Arguments:
            sentence --- a sentenced query
        Returns:
            An array of document (doc objects)
        """
        sentence = sentence.strip()
        sentence = strip_accents(sentence)

        if sentence == u"":
            return self.docs

        result_list_list = []
        total_results = 0

        for query_parser in self.search_param_list[search_type]:
            query = query_parser["query_parser"].parse(sentence)
            if must_sort and "sortedby" in query_parser:
                result_list = self.__searcher.search(
                    query, limit=limit, sortedby=query_parser["sortedby"])
            else:
                result_list = self.__searcher.search(
                    query, limit=limit)

            result_list_list.append(result_list)
            total_results += len(result_list)

            if not must_sort and total_results >= limit:
                break

        # merging results
        results = result_list_list[0]
        for result_intermediate in result_list_list[1:]:
            results.extend(result_intermediate)

        docs = [self._docs_by_id.get(result['docid']) for result in results]
        try:
            while True:
                docs.remove(None)
        except ValueError:
            pass
        assert (None not in docs)

        if limit is not None:
            docs = docs[:limit]

        return docs

    def find_suggestions(self, sentence):
        """
        Search all possible suggestions. Suggestions returned always have at
        least one document matching.

        Arguments:
            sentence --- keywords (single strings) for which we want
                suggestions
        Return:
            An array of sets of keywords. Each set of keywords (-> one string)
            is a suggestion.
        """
        if not isinstance(sentence, unicode):
            sentence = unicode(sentence, encoding="UTF-8")

        keywords = sentence.split(" ")
        final_suggestions = []

        corrector = self.__searcher.corrector("content")
        label_corrector = self.__searcher.corrector("label")
        for keyword_idx in range(0, len(keywords)):
            keyword = keywords[keyword_idx]
            if (len(keyword) <= MIN_KEYWORD_LEN):
                continue
            keyword_suggestions = label_corrector.suggest(keyword, limit=2)[:]
            keyword_suggestions += corrector.suggest(keyword, limit=5)[:]
            for keyword_suggestion in keyword_suggestions:
                new_suggestion = keywords[:]
                new_suggestion[keyword_idx] = keyword_suggestion
                new_suggestion = u" ".join(new_suggestion)

                docs = self.find_documents(
                    new_suggestion, limit=1, must_sort=False,
                    search_type='strict'
                )
                if len(docs) <= 0:
                    continue
                final_suggestions.append(new_suggestion)
        final_suggestions.sort()
        return final_suggestions

    def create_label(self, label, doc=None, callback=dummy_progress_cb):
        """
        Create a new label

        Arguments:
            doc --- first document on which the label must be added (required
                    for now)
        """
        label = copy.copy(label)
        assert(label not in self.labels.values())
        self.labels[label.name] = label
        self.label_guesser.load(label.name)
        if doc:
            doc.add_label(label)
            updater = self.get_index_updater(optimize=False)
            updater.upd_doc(doc)
            updater.commit()

    def add_label(self, doc, label, update_index=True):
        """
        Add a label on a document.

        Arguments:
            label --- The new label (see labels.Label)
            doc --- The first document on which this label has been added
        """
        label = copy.copy(label)
        assert(label in self.labels.values())
        doc.add_label(label)
        if update_index:
            updater = self.get_index_updater(optimize=False)
            updater.upd_doc(doc)
            updater.commit()

    def remove_label(self, doc, label, update_index=True):
        """
        Remove a label from a doc. Takes care of updating the index
        """
        doc.remove_label(label)
        if update_index:
            updater = self.get_index_updater(optimize=False)
            updater.upd_doc(doc)
            updater.commit()

    def update_label(self, old_label, new_label, callback=dummy_progress_cb):
        """
        Replace 'old_label' by 'new_label' on all the documents. Takes care of
        updating the index.
        """
        assert(old_label)
        assert(new_label)
        self.labels.pop(old_label.name)
        if new_label not in self.labels.values():
            self.labels[new_label.name] = new_label
        current = 0
        total = len(self.docs)
        updater = self.get_index_updater(optimize=False)
        for doc in self.docs:
            must_reindex = (old_label in doc.labels)
            callback(current, total, self.LABEL_STEP_UPDATING, doc)
            doc.update_label(old_label, new_label)
            if must_reindex:
                updater.upd_doc(doc)
            current += 1

        updater.commit()

    def destroy_label(self, label, callback=dummy_progress_cb):
        """
        Remove the label 'label' from all the documents. Takes care of updating
        the index.
        """
        assert(label)
        self.labels.pop(label.name)
        current = 0
        docs = self.docs
        total = len(docs)
        updater = self.get_index_updater(optimize=False)
        for doc in docs:
            must_reindex = (label in doc.labels)
            callback(current, total, self.LABEL_STEP_DESTROYING, doc)
            doc.remove_label(label)
            if must_reindex:
                updater.upd_doc(doc)
            current += 1
        updater.commit()

    def reload_searcher(self):
        """
        When the index has been updated, it's safer to re-instantiate the
        Whoosh.
        Searcher object used to browse it.

        You shouldn't have to call this method yourself.
        """
        searcher = self.__searcher
        self.__searcher = self.index.searcher()
        del(searcher)

    def destroy_index(self):
        """
        Destroy the index. Don't use this DocSearch object anymore after this
        call. Next instantiation of a DocSearch will rebuild the whole index
        """
        logger.info("Destroying the index ...")
        rm_rf(self.indexdir)
        rm_rf(self.label_guesser_dir)
        logger.info("Done")

    def is_hash_in_index(self, filehash):
        """
        Check if there is a document using this file hash
        """
        filehash = (u"%X" % filehash)
        results = self.__searcher.search(
            whoosh.query.Term('docfilehash', filehash))
        return results

    def __get_label_list(self):
        labels = [label for label in self.labels.values()]
        labels.sort()
        return labels

    def __set_label_list(self, label_list):
        for label in label_list:
            self.label_guesser.load(label.name)
        labels = {label.name: label for label in label_list}
        self.labels = labels

    label_list = property(__get_label_list, __set_label_list)
