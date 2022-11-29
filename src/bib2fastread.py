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
        self.mandatory_fields = set()
        self.default_field_value = dict()
        self.entry_types = set()

    def add_entry_type(self, entry_type):
        self.entry_types.add(entry_type)

    def add_field(self, header_name, fields, default_value, is_mandatory = False):
        if not header_name in self.fields:
            self.fields[header_name] = []
            self.default_field_value[header_name] = default_value
        if fields is not None:
            self.fields[header_name] += fields
        if is_mandatory:
            self.mandatory_fields.add(header_name)

    def set_input_file(self, bibtex_filename):
        self.input_bibtex_file = bibtex_filename

    def set_output_csv_file(self, csv_filename):
        self.output_csv_file = csv_filename

    def run(self):
        parser = BibTexParser(common_strings = True, ignore_nonstandard_types = False, homogenize_fields = False, customization = add_plaintext_fields, add_missing_from_crossref = True)
        with open(self.input_bibtex_file) as bibtex_input_file:
            try:
                raw_database = parser.parse_file(bibtex_input_file)
            except UnicodeDecodeError:
                with open(self.input_bibtex_file, encoding = "latin-1") as bibtex_input_file_fallback:
                    raw_database = parser.parse_file(bibtex_input_file_fallback) 
        final_database = BibDatabase()

        with open(self.output_csv_file, 'w', newline='') as csvfile:
            csvwriter = csv.writer(csvfile, delimiter=',',quotechar='"', quoting=csv.QUOTE_MINIMAL)
            csv_headers = self.fields.keys()
            csvwriter.writerow(csv_headers)
            for entry in raw_database.entries:
                if not self.entry_types or entry["ENTRYTYPE"] in self.entry_types:
                    csv_row = []
                    entry_status = True
                    for header, fields in self.fields.items():
                        field_status = False
                        for field in fields:
                            try:
                                if entry[field]:
                                    field_status = True
                                    if field == 'doi':
                                        csv_row.append('https://dx.doi.org/' + entry[field])
                                    else:
                                        csv_row.append(entry[field])
                                    break
                            except KeyError:
                                pass
                        else:
                            if entry_status:
                                if not field_status and header in self.mandatory_fields:
                                    print("Error: insufficient data for entry: ", entry["ID"], entry["ENTRYTYPE"])
                                    entry_status = False
                                else:
                                    csv_row.append(self.default_field_value[header])
                    if entry_status:                
                         csvwriter.writerow(csv_row)

if __name__ == "__main__":
    bibtex_filename = sys.argv[1]
    basename = os.path.basename(bibtex_filename)
    (filename, extension) = os.path.splitext(basename)
    bibtex_filter = BibtexFilter()
    bibtex_filter.set_input_file(bibtex_filename)
    bibtex_filter.set_output_csv_file(filename + '.csv')
    bibtex_filter.add_entry_type('inproceedings')
    bibtex_filter.add_entry_type('article')
    bibtex_filter.add_field('Document Title', ['title'], '', True)
    bibtex_filter.add_field('Abstract', ['abstract'], '')
    bibtex_filter.add_field('Year', ['year'], 0)
    bibtex_filter.add_field('PDF Link',['doi', 'url'], '')
    bibtex_filter.run()
