#!/usr/bin/python3

import csv
import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.bwriter import BibTexWriter
from bibtexparser.bibdatabase import BibDatabase
from bibtexparser.customization import *

import os.path
import sys

class BibtexFilter(object):

    def __init__(self):
        self.input_bibtex_file = None
        self.output_bibtex_file = None
        self.output_csv_file = None
        self.search_terms = {}
        self.search_fields = []

    def has_term(self, entry, field, term):
        try:
            if term.casefold() in entry[field].casefold():
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
        parser = BibTexParser(ignore_nonstandard_types = False, homogenize_fields = False, common_strings = True, customization = add_plaintext_fields)
        with open(self.input_bibtex_file) as bibtex_input_file:
            raw_database = parser.parse_file(bibtex_input_file)
        final_database = BibDatabase()

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
                try:
                    csv_row = [entry['ID'], entry['doi'], entry['plain_title'], entry['plain_abstract']]
                except:
                    csv_row = [entry['ID'], '', entry['plain_title'], '']
                for category in search_results.keys():
                    csv_row.append(search_results[category])
                csvwriter.writerow(csv_row)

                writer = BibTexWriter()
                writer.indent = '    '
                with open(self.output_bibtex_file, 'w') as bibtex_output_file:
                    bibtex_output_file.write(writer.write(final_database))


if __name__ == "__main__":
    bibtex_filename = sys.argv[1]
    basename = os.path.basename(bibtex_filename)
    (filename, extension) = os.path.splitext(basename)
    bibtex_filter = BibtexFilter()
    bibtex_filter.set_input_file(bibtex_filename)
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
