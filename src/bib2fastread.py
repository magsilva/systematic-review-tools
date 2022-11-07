#!/usr/bin/python3

import csv
import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.bibdatabase import BibDatabase
from bibtexparser.customization import *

import os.path
import sys

class BibtexFilter(object):

    def __init__(self):
        self.input_bibtex_file = None
        self.output_csv_file = None
        self.fields = dict()
        self.default_field_value = dict()

    def add_field(self, header_name, fields, default_value):
        if not header_name in self.fields:
            self.fields[header_name] = []
            self.default_field_value[header_name] = default_value
        if fields is not None:
            self.fields[header_name] += fields

    def set_input_file(self, bibtex_filename):
        self.input_bibtex_file = bibtex_filename

    def set_output_csv_file(self, csv_filename):
        self.output_csv_file = csv_filename

    def run(self):
        parser = BibTexParser(ignore_nonstandard_types = False, homogenize_fields = False, common_strings = True, customization = add_plaintext_fields)
        with open(self.input_bibtex_file) as bibtex_input_file:
            raw_database = parser.parse_file(bibtex_input_file)
        final_database = BibDatabase()

        with open(self.output_csv_file, 'w', newline='') as csvfile:
            csvwriter = csv.writer(csvfile, delimiter=',',quotechar='"', quoting=csv.QUOTE_MINIMAL)
            csv_headers = self.fields.keys()
            csvwriter.writerow(csv_headers)
            for entry in raw_database.entries:
                csv_row = []
                for header, fields in self.fields.items():
                    for field in fields:
                        try:
                            if entry[field]:
                                if field == 'doi':
                                    csv_row.append('https://dx.doi.org/' + entry[field])
                                else:
                                    csv_row.append(entry[field])
                                break
                        except KeyError:
                            pass
                    else:
                        csv_row.append(self.default_field_value[header])
                csvwriter.writerow(csv_row)

if __name__ == "__main__":
    bibtex_filename = sys.argv[1]
    basename = os.path.basename(bibtex_filename)
    (filename, extension) = os.path.splitext(basename)
    bibtex_filter = BibtexFilter()
    bibtex_filter.set_input_file(bibtex_filename)
    bibtex_filter.set_output_csv_file(filename + '.csv')
    bibtex_filter.add_field('Document Title', ['title'], '')
    bibtex_filter.add_field('Abstract', ['abstract'], '')
    bibtex_filter.add_field('Year', ['year'], 0)
    bibtex_filter.add_field('PDF Link',['doi', 'url'], '')
    bibtex_filter.add_field('label', ['paper_is_selected'], 'no')
    bibtex_filter.run()
