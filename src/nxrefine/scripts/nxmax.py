#!/usr/bin/env python
# -----------------------------------------------------------------------------
# Copyright (c) 2022, Argonne National Laboratory.
#
# Distributed under the terms of an Open Source License.
#
# The full license is in the file LICENSE.pdf, distributed with this software.
# -----------------------------------------------------------------------------

import argparse

from nxrefine.nxreduce import NXMultiReduce, NXReduce


def main():

    parser = argparse.ArgumentParser(
        description="Find maximum counts of the signal in the specified path")
    parser.add_argument('-d', '--directory', required=True,
                        help='scan directory')
    parser.add_argument('-e', '--entries', nargs='+',
                        help='names of entries to be processed')
    parser.add_argument('-f', '--first', type=int, help='first frame')
    parser.add_argument('-l', '--last', type=int, help='last frame')
    parser.add_argument('-o', '--overwrite', action='store_true',
                        help='overwrite existing maximum')
    parser.add_argument('-m', '--monitor', action='store_true',
                        help='monitor progress in the command line')
    parser.add_argument('-q', '--queue', action='store_true',
                        help='add to server task queue')

    args = parser.parse_args()

    if args.entries:
        for entry in args.entries:
            reduce = NXReduce(entry, args.directory, max=True,
                              first=args.first, last=args.last,
                              overwrite=args.overwrite,
                              monitor_progress=args.monitor)
        if args.queue:
            reduce.queue('nxmax', args)
        else:
            reduce.nxmax()
    else:
        reduce = NXMultiReduce(args.directory, max=True,
                               first=args.first, last=args.last,
                               overwrite=args.overwrite,
                               monitor_progress=args.monitor)
        if args.queue:
            reduce.queue('nxmax')
        else:
            reduce.nxmax()


if __name__ == "__main__":
    main()
