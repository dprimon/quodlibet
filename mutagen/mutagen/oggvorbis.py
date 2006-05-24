# Ogg Vorbis support, sort of.
#
# Copyright 2006 Joe Wreschnig <piman@sacredchao.net>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# $Id$

"""Read and write Ogg Vorbis comments.

This module can handle Vorbis streams in any Ogg file (though it only
finds and manipulates the first one; if you need better logical stream
control, use OggPage directly). This means it can read, tag, and get
information about e.g. OGM files with a Vorbis stream.

Read more about Ogg Vorbis at http://vorbis.com/. This module is based
off the specification at http://www.xiph.org/ogg/doc/rfc3533.txt.
"""

from cStringIO import StringIO

from mutagen import FileType, Metadata
from mutagen._vorbis import VCommentDict
from mutagen.ogg import OggPage
from mutagen._util import cdata

class error(IOError): pass
class OggVorbisNoHeaderError(error): pass

class OggVorbisInfo(object):
    """Ogg Vorbis stream information.

    Attributes:
    length - file length in seconds, as a float
    bitrate - nominal ('average') bitrate in bits per second, as an int
    """
    def __init__(self, fileobj):
        page = OggPage(fileobj)
        while not page.packets[0].startswith("\x01vorbis"):
            page = OggPage(fileobj)
        if not page.first:
            raise IOError("page has ID header, but doesn't start a packet")
        self.channels = ord(page.packets[0][11])
        self.sample_rate = cdata.uint_le(page.packets[0][12:16])
        self.serial = page.serial

        max_bitrate = cdata.uint_le(page.packets[0][16:20])
        nominal_bitrate = cdata.uint_le(page.packets[0][20:24])
        min_bitrate = cdata.uint_le(page.packets[0][24:28])
        if nominal_bitrate == 0:
            self.bitrate = (max_bitrate + min_bitrate) // 2
        elif max_bitrate:
            # If the max bitrate is less than the nominal, we know
            # the nominal is wrong.
            self.bitrate = min(max_bitrate, nominal_bitrate)
        elif min_bitrate:
            self.bitrate = max(min_bitrate, nominal_bitrate)
        else:
            self.bitrate = nominal_bitrate

    def pprint(self):
        return "Ogg Vorbis, %.2f seconds, %d bps" % (self.length, self.bitrate)

class OggVCommentDict(VCommentDict):
    """Vorbis comments embedded in an Ogg bitstream."""

    def __init__(self, fileobj, info):
        pages = []
        complete = False
        while not complete:
            page = OggPage(fileobj)
            if page.serial == info.serial:
                pages.append(page)
                complete = page.complete or (len(page.packets) > 1)
        data = OggPage.to_packets(pages)[0][7:] # Strip off "\x03vorbis".
        super(OggVCommentDict, self).__init__(data)

    def _inject(self, fileobj, offset=0):
        """Write tag data into the Vorbis comment packet/page."""
        fileobj.seek(offset)

        # Find the old pages in the file; we'll need to remove them,
        # plus grab any stray setup packet data out of them.
        old_pages = []
        page = OggPage(fileobj)
        while not page.packets[0].startswith("\x03vorbis"):
            page = OggPage(fileobj)
        old_pages.append(page)
        while not page.packets[-1].startswith("\x05vorbis"):
            page = OggPage(fileobj)
            if page.serial == old_pages[0].serial:
                old_pages.append(page)

        # We will have the comment data, and the setup packet for sure.
        # Ogg Vorbis I says there won't be another one until at least
        # one more page.
        packets = OggPage.to_packets(old_pages)
        assert(len(packets) == 2)

        # Set the new comment packet.
        packets[0] = "\x03vorbis" + self.write()

        # Render the new pages, copying the header from the old ones.
        new_pages = OggPage.from_packets(packets, old_pages[0].sequence)
        for page in new_pages:
            page.serial = old_pages[0].serial
        new_pages[-1].complete = old_pages[-1].complete
        new_data = "".join(map(OggPage.write, new_pages))

        # Make room in the file for the new data.
        delta = len(new_data)
        fileobj.seek(old_pages[0].offset, 0)
        Metadata._insert_space(fileobj, delta, old_pages[0].offset)
        fileobj.seek(old_pages[0].offset, 0)
        fileobj.write(new_data)
        new_data_end = fileobj.tell()

        # Go through the old pages and delete them. Since we shifted
        # the data down the file, we need to adjust their offsets. We
        # also need to go backwards, so we don't adjust the deltas of
        # the other pages.
        old_pages.reverse()
        for old_page in old_pages:
            adj_offset = old_page.offset + delta
            Metadata._delete_bytes(fileobj, old_page.size, adj_offset)

        # Finally, if there's any discrepency in length, we need to
        # renumber the pages for the logical stream.
        if len(old_pages) != len(new_pages):
            fileobj.seek(new_data_end, 0)
            serial = new_pages[-1].serial
            sequence = new_pages[-1].sequence + 1
            OggPage.renumber(fileobj, serial, sequence)

class OggVorbis(FileType):
    """An Ogg Vorbis file."""

    def __init__(self, filename=None):
        if filename is not None:
            self.load(filename)

    def score(filename, fileobj, header):
        return (header.startswith("OggS") + ("\x01vorbis" in header))
    score = staticmethod(score)

    def load(self, filename):
        """Load file information from a filename."""

        self.filename = filename
        fileobj = file(filename, "rb")
        try:
            try:
                self.info = OggVorbisInfo(fileobj)
                self.tags = OggVCommentDict(fileobj, self.info)

                # For non-muxed streams, look at the last page.
                try: fileobj.seek(-256*256, 2)
                except IOError:
                    # The file is less than 64k in length.
                    fileobj.seek(0)
                data = fileobj.read()
                try: index = data.rindex("OggS")
                except ValueError:
                    raise OggVorbisNoHeaderError(
                        "unable to find final Ogg header")
                stringobj = StringIO(data[index:])
                last_page = OggPage(stringobj)
                if last_page.serial == self.info.serial:
                    samples = last_page.position
                else:
                    # The stream is muxed, so use the slow way.
                    fileobj.seek(0)
                    page = OggPage(fileobj)
                    samples = page.position
                    while not page.last:
                        while page.serial != self.info.serial:
                            page = OggPage(fileobj)
                        if page.serial == self.info.serial:
                            samples = max(samples, page.position)

                self.info.length = samples / float(self.info.sample_rate)

            except IOError, e:
                raise OggVorbisNoHeaderError(e)
        finally:
            fileobj.close()

    def delete(self, filename=None):
        """Remove tags from a file.

        If no filename is given, the one most recently loaded is used.
        """
        if filename is None:
            filename = self.filename

        self.tags.clear()
        fileobj = file(filename, "rb+")
        try:
            try: self.tags._inject(fileobj)
            except IOError, e:
                raise OggVorbisNoHeaderError(e)
        finally:
            fileobj.close()

    def save(self, filename=None):
        """Save a tag to a file.

        If no filename is given, the one most recently loaded is used.
        """
        if filename is None:
            filename = self.filename
        self.tags.validate()
        fileobj = file(filename, "rb+")
        try:
            try: self.tags._inject(fileobj)
            except IOError, e:
                raise OggVorbisNoHeaderError(e)
        finally:
            fileobj.close()

Open = OggVorbis

def delete(filename):
    """Remove tags from a file."""
    OggVorbis(filename).delete()
