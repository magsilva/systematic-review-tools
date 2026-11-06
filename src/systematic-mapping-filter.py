#!/usr/bin/python3

import csv
from bibtex_io import read_bibtex, new_database, write_bibtex

import os.path
import sys

import re
import unicodedata

class BibtexLinter(object):

    def __init__(self):
        self.input_bibtex_file = None
        self.output_bibtex_file = None

    def normalize_id(self, text):
        text = unicodedata.normalize("NFKD", text)
        text = text.encode("ascii", "ignore").decode("ascii")
        text = re.sub(r"\s+", "", text)
        return text

    def set_input_file(self, bibtex_filename):
        self.input_bibtex_file = bibtex_filename

    def set_output_file(self, bibtex_filename):
        self.output_bibtex_file = bibtex_filename

    def fix(self):
        database = read_bibtex(self.input_bibtex_file)
        keys = {entry['ID']: self.normalize_id(entry['ID']) for entry in database.entries}
        if not all(keys.values()) or len(set(keys.values())) != len(keys):
            raise ValueError('Normalizing citation keys produced empty or duplicate keys.')
        for entry in database.entries:
            entry['ID'] = keys[entry['ID']]
            for field in ('crossref', 'xref'):
                if field in entry:
                    entry[field] = keys.get(entry[field], entry[field])
        write_bibtex(database, self.output_bibtex_file)


class BibtexFilter(object):

    def __init__(self):
        self.input_bibtex_file = None
        self.output_bibtex_file = None
        self.output_csv_file = None
        self.search_terms = {}
        self.search_fields = []
        self.casefold_term = True

    def has_term(self, entry, field, term):
        try:
            if self.casefold_term:
                term = term.casefold()
                field_content = entry[field].casefold()
            else:
                field_content = entry[field]
            if term in field_content:
                return True
        except KeyError:
            pass
        return False

    def add_search_field(self, field):
        self.search_fields.append(field)

    def add_search_terms(self, category, terms):
        self.search_terms[category] = terms

    def set_input_file(self, bibtex_filename):
        self.input_bibtex_file = bibtex_filename

    def set_output_csv_file(self, csv_filename):
        self.output_csv_file = csv_filename

    def set_output_bibtex_file(self, bibtex_filename):
        self.output_bibtex_file = bibtex_filename

    def apply_filters(self):
        raw_database = read_bibtex(self.input_bibtex_file, plaintext=True)
        final_database = new_database(preambles=raw_database.preambles)

        with open(self.output_csv_file, 'w', newline='') as csvfile:
            csvwriter = csv.writer(csvfile, delimiter=',',quotechar='"', quoting=csv.QUOTE_MINIMAL)
            csv_fields = ['id', 'doi', 'title', 'abstract'];
            for category in self.search_terms.keys():
                csv_fields.append(category)
            csvwriter.writerow(csv_fields)
            for entry in raw_database.entries:
                search_results = {}
                for category in self.search_terms.keys():
                    search_results[category] = False
                    for field in self.search_fields:
                        for term in self.search_terms[category]:
                            search_results[category] |= self.has_term(entry, 'plain_' +  field, term)

                veredict = True
                for result in search_results.values():
                    veredict &= result
                if veredict:
                    final_database.entries.append(entry)
                else:
                    print("Ignoring paper", entry['ID'])

                try:
                    csv_row = [entry['ID'], entry['doi'], entry['plain_title'], entry['plain_abstract']]
                except:
                    csv_row = [entry['ID'], '', entry['plain_title'], '']
                for category in search_results.keys():
                    csv_row.append(search_results[category])
                csvwriter.writerow(csv_row)

        write_bibtex(final_database, self.output_bibtex_file, indent='    ')


if __name__ == "__main__":
    bibtex_filename = sys.argv[1]
    basename = os.path.basename(bibtex_filename)
    (filename, extension) = os.path.splitext(basename)

    bibtex_linter = BibtexLinter()
    bibtex_linter.set_input_file(bibtex_filename)
    bibtex_linter.set_output_file(filename + '.clean.bib')
    bibtex_linter.fix()

    bibtex_filter = BibtexFilter()
    bibtex_filter.set_input_file(filename + '.clean.bib')
    bibtex_filter.set_output_csv_file(filename + '.csv')
    bibtex_filter.set_output_bibtex_file(filename + '.filtered.bib')
    bibtex_filter.add_search_field('title')
    bibtex_filter.add_search_field('abstract')
    bibtex_filter.add_search_field('keywords')
    bibtex_filter.add_search_terms('population', ['', ''])
    bibtex_filter.add_search_terms('intervention', ['', ''])
    bibtex_filter.add_search_terms('control', ['', ''])
    bibtex_filter.add_search_terms('outcome', ['', ''])
    bibtex_filter.apply_filters()
