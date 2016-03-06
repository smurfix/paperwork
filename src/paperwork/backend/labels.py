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
Code to manage document labels
"""
import os

from gi.repository import Gdk
import simplebayes

from paperwork.backend.util import mkdir_p
from paperwork.backend.util import strip_accents


class Label(object):

    """
    Represents a Label (color + string).
    """

    def __init__(self, name=u"", color="#000000000000"):
        """
        Arguments:
            name --- label name
            color --- label color (string representation, see get_color_str())
        """
        if type(name) == unicode:
            self.name = name
        else:
            self.name = unicode(name, encoding='utf-8')
        self.color = Gdk.RGBA()
        self.color.parse(color)

    def __copy__(self):
        return Label(self.name, self.get_color_str())

    def __label_cmp(self, other, text_only=False):
        """
        Comparaison function. Can be used to sort labels alphabetically.

        Labels are deemed equal if they have the same (or similar) text,
        regardless of color.
        """
        if other is None:
            return -1
        label_name = strip_accents(self.name).lower()
        other_name = strip_accents(other.name).lower()
        cmp_r = cmp(label_name, other_name)
        if cmp_r != 0 or text_only:
            return cmp_r
        return cmp(self.get_color_str(), other.get_color_str())

    def __lt__(self, other):
        return self.__label_cmp(other) < 0

    def __gt__(self, other):
        return self.__label_cmp(other) > 0

    def __eq__(self, other):
        return self.__label_cmp(other, True) == 0

    def __le__(self, other):
        return self.__label_cmp(other) <= 0

    def __ge__(self, other):
        return self.__label_cmp(other) >= 0

    def __ne__(self, other):
        return self.__label_cmp(other, True) != 0

    def __hash__(self):
        return hash(self.name)

    def get_html_color(self):
        """
        get a string representing the color, using HTML notation
        """
        return ("#%02x%02x%02x" % (self.color.red, self.color.green,
                                   self.color.blue))

    def get_color_str(self):
        """
        Returns a string representation of the color associated to this label.
        """
        return self.color.to_string()

    def get_html(self):
        """
        Returns a HTML string that represent the label. Can be used with GTK.
        """
        return ("<span bgcolor=\"%s\">    </span> %s"
                % (self.get_html_color(), self.name))

    def get_rgb_fg(self):
        bg_color = self.get_rgb_bg()
        brightness = (((bg_color[0] * 255) * 0.299) +
                      ((bg_color[1] * 255) * 0.587) +
                      ((bg_color[2] * 255) * 0.114))
        if brightness > 186:
            return (0.0, 0.0, 0.0)  # black
        else:
            return (1.0, 1.0, 1.0)  # white

    def get_rgb_bg(self):
        return (self.color.red, self.color.green, self.color.blue)

    def __str__(self):
        return self.name

    def __repr__(self):
        return ("Label(name=%s,color=%s)"
                % (repr(self.name), self.get_html_color()))


class LabelGuessUpdater(object):
    def __init__(self, guesser):
        self.guesser = guesser
        self.updated_docs = set()

    def _get_doc_txt(self, doc):
        if doc.nb_pages <= 0:
            return u""
        if not doc.can_edit:
            # document always come with all its pages
            return doc.text.strip()
        # document is added page per page --> the first page only
        # is used for evaluation
        txt = doc.pages[0].text
        txt = u"\n".join(txt)
        txt = txt.strip()
        return txt

    def add_doc(self, doc):
        doc_txt = self._get_doc_txt(doc)
        if doc_txt == u"":
            return
        doc_txt = doc_txt.encode("utf-8")

        labels = {label.name for label in doc.labels}

        # just in case, make sure all the labels are loaded
        for label in labels:
            self.guesser.load(label)

        for (label, guesser) in self.guesser._bayes.iteritems():
            value = "yes" if label in labels else "no"
            guesser.train(value, doc_txt)

        self.updated_docs.add(doc)

    def upd_doc(self, doc):
        doc_txt = self._get_doc_txt(doc)
        if doc_txt == u"":
            return
        doc_txt = doc_txt.encode("utf-8")

        new_labels = {label.name for label in doc.labels}
        old_labels = {label.name for label in doc._previous_labels}

        for new_label in new_labels:
            if new_label in old_labels:
                # unchanged
                continue
            # just in case, make sure all the labels are loaded
            self.guesser.load(new_label)
            guesser = self.guesser._bayes[new_label]
            guesser.untrain("no", doc_txt)
            guesser.train("yes", doc_txt)

        for old_label in old_labels:
            if old_label in new_labels:
                # unchanged
                continue
            # just in case, make sure all the labels are loaded
            self.guesser.load(old_label)
            guesser = self.guesser._bayes[old_label]
            guesser.untrain("yes", doc_txt)
            guesser.train("no", doc_txt)

    def del_doc(self, doc):
        doc_txt = self._get_doc_txt(doc)
        if doc_txt == u"":
            return
        doc_txt = doc_txt.encode("utf-8")

        labels = {label.name for label in doc._previous_labels}

        # just in case, make sure all the labels are loaded
        for label in labels:
            self.guesser.load(label)

        for (label, guesser) in self.guesser._bayes.iteritems():
            value = "yes" if label in labels else "no"
            guesser.untrain(value, doc_txt)

        self.updated_docs.add(doc)

    def commit(self):
        for baye in self.guesser._bayes.values():
            baye.cache_persist()
        for doc in self.updated_docs:
            # Acknowledge the new labels
            doc._previous_labels = doc.labels.copy()
        self.updated_docs = set()

    def cancel(self):
        names = [x for x in self.guesser._bayes.keys()]  # copy
        for label_name in names:
            self.guesser.load(label_name, force_reload=True)
        self.updated_docs = set()


class LabelGuesser(object):
    WEIGHT_YES = 5.0
    WEIGHT_NO = 1.0

    def __init__(self, bayes_dir):
        self._bayes_dir = bayes_dir
        self._bayes = {}

    def load(self, label_name, force_reload=False):
        label_hash = hex(abs(hash(label_name)))[2:]
        baye_dir = os.path.join(self._bayes_dir, label_hash)
        mkdir_p(baye_dir)
        if label_name not in self._bayes or force_reload:
            self._bayes[label_name] = simplebayes.SimpleBayes(
                cache_path=baye_dir
            )
            self._bayes[label_name].cache_train()

    def get_updater(self):
        return LabelGuessUpdater(self)

    def guess(self, doc):
        doc_txt = doc.text
        if doc_txt == u"":
            return set()
        doc_txt = doc_txt.encode("utf-8")
        label_names = set()
        for (label_name, guesser) in self._bayes.iteritems():
            # we balance ourselves the scores, otherwise 'no' wins
            # too easily
            scores = guesser.score(doc_txt)
            yes = scores['yes'] if 'yes' in scores else 0.0
            no = scores['no'] if 'no' in scores else 0.0
            if yes * self.WEIGHT_YES > no * self.WEIGHT_NO:
                label_names.add(label_name)
        return label_names
