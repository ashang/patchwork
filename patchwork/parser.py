# Patchwork - automated patch tracking system
# Copyright (C) 2008 Jeremy Kerr <jk@ozlabs.org>
#
# This file is part of the Patchwork package.
#
# Patchwork is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# Patchwork is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Patchwork; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

import codecs
import datetime
from email.header import decode_header
from email.header import make_header
from email.utils import mktime_tz
from email.utils import parsedate_tz
from fnmatch import fnmatch
import logging
import re

from django.contrib.auth.models import User
from django.utils import six

from patchwork.models import Comment
from patchwork.models import CoverLetter
from patchwork.models import DelegationRule
from patchwork.models import get_default_initial_patch_state
from patchwork.models import Patch
from patchwork.models import Person
from patchwork.models import Project
from patchwork.models import Series
from patchwork.models import SeriesReference
from patchwork.models import SeriesPatch
from patchwork.models import State
from patchwork.models import Submission


_hunk_re = re.compile(r'^\@\@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? \@\@')
_filename_re = re.compile(r'^(---|\+\+\+) (\S+)')
list_id_headers = ['List-ID', 'X-Mailing-List', 'X-list']

logger = logging.getLogger(__name__)


def normalise_space(value):
    whitespace_re = re.compile(r'\s+')
    return whitespace_re.sub(' ', value).strip()


def sanitise_header(header_contents, header_name=None):
    """Clean and individual mail header.

    Given a header with header_contents, optionally labelled
    header_name, decode it with decode_header, sanitise it to make
    sure it decodes correctly and contains no invalid characters,
    then encode the result with make_header()
    """

    # We have some Py2/Py3 issues here.
    #
    # Firstly, the email parser (before we get here)
    # Python 3: headers with weird chars are email.header.Header
    #           class, others as str
    # Python 2: every header is an str
    #
    # Secondly, the behaviour of decode_header:
    # Python 3: weird headers are labelled as unknown-8bit
    # Python 2: weird headers are not labelled differently
    #
    # Lastly, aking matters worse, in Python2, unknown-8bit doesn't
    # seem to be supported as an input to make_header, so not only do
    # we have to detect dodgy headers, we have to fix them ourselves.
    #
    # We solve this by catching any Unicode errors, and then manually
    # handling any interesting headers.

    value = decode_header(header_contents)
    try:
        header = make_header(value,
                             header_name=header_name,
                             continuation_ws='\t')
    except UnicodeDecodeError:
        # At least one of the parts cannot be encoded as ascii.
        # Find out which one and fix it somehow.
        #
        # We get here under Py2 when there's non-7-bit chars in header,
        # or under Py2 or Py3 where decoding with the coding hint fails.

        new_value = []

        for (part, coding) in value:
            # We have random bytes that aren't properly coded.
            # If we had a coding hint, it failed to help.
            if six.PY3:
                # python3 - force coding to unknown-8bit
                new_value += [(part, 'unknown-8bit')]
            else:
                # python2 - no support in make_header for unknown-8bit
                # We should do unknown-8bit coding ourselves.
                # For now, we're just going to replace any dubious
                # chars with ?.
                #
                # TODO: replace it with a proper QP unknown-8bit codec.
                new_value += [(part.decode('ascii', errors='replace')
                               .encode('ascii', errors='replace'),
                               None)]

        header = make_header(new_value,
                             header_name=header_name,
                             continuation_ws='\t')

    return header


def clean_header(header):
    """Decode (possibly non-ascii) headers."""

    sane_header = sanitise_header(header)

    # on Py2, we want to do unicode(), on Py3, str().
    # That gets us the decoded, un-wrapped header.
    if six.PY2:
        header_str = unicode(sane_header)
    else:
        header_str = str(sane_header)

    return normalise_space(header_str)


def find_project_by_id(list_id):
    """Find a `project` object with given `list_id`."""
    project = None
    try:
        project = Project.objects.get(listid=list_id)
    except Project.DoesNotExist:
        pass
    return project


def find_project_by_header(mail):
    project = None
    listid_res = [re.compile(r'.*<([^>]+)>.*', re.S),
                  re.compile(r'^([\S]+)$', re.S)]

    for header in list_id_headers:
        if header in mail:

            for listid_re in listid_res:
                match = listid_re.match(mail.get(header))
                if match:
                    break

            if not match:
                continue

            listid = match.group(1)

            project = find_project_by_id(listid)
            if project:
                break

    return project


def find_series(mail):
    """Find a patch's series.

    Traverse RFC822 headers, starting with most recent first, to find
    ancestors and the related series. Headers are traversed in reverse
    to handle series sent in reply to previous series, like so:

        [PATCH 0/3] A cover letter
          [PATCH 1/3] The first patch
          ...
          [PATCH v2 0/3] A cover letter
            [PATCH v2 1/3] The first patch
            ...

    This means we evaluate headers like so:

    - first, check for a Series that directly matches this message's
      Message-ID
    - then, check for a series that matches the In-Reply-To
    - then, check for a series that matches the References, from most
      recent (the patch's closest ancestor) to least recent

    Args:
        mail (email.message.Message): The mail to extract series from

    Returns:
        The matching ``Series`` instance, if any
    """
    for ref in [mail.get('Message-Id')] + find_references(mail):
        # try parsing by RFC5322 fields first
        try:
            return SeriesReference.objects.get(msgid=ref).series
        except SeriesReference.DoesNotExist:
            pass


def find_author(mail):
    from_header = clean_header(mail.get('From'))
    name, email = (None, None)

    # tuple of (regex, fn)
    #  - where fn returns a (name, email) tuple from the match groups resulting
    #    from re.match().groups()
    # TODO(stephenfin): Perhaps we should check for "real" email addresses
    # instead of anything ('.*?')
    from_res = [
        # for "Firstname Lastname" <example@example.com> style addresses
        (re.compile(r'"?(.*?)"?\s*<([^>]+)>'), (lambda g: (g[0], g[1]))),

        # for example at example.com (Firstname Lastname) style addresses
        (re.compile(r'(.*?)\sat\s(.*?)\s*\(([^\)]+)\)'),
         (lambda g: (g[2], '@'.join(g[0:2])))),

        # for example@example.com (Firstname Lastname) style addresses
        (re.compile(r'"?(.*?)"?\s*\(([^\)]+)\)'), (lambda g: (g[1], g[0]))),

        # everything else
        (re.compile(r'(.*)'), (lambda g: (None, g[0]))),
    ]

    for regex, fn in from_res:
        match = regex.match(from_header)
        if match:
            (name, email) = fn(match.groups())
            break

    if not email:
        raise ValueError("Invalid 'From' header")

    email = email.strip()
    if name is not None:
        name = name.strip()

    try:
        person = Person.objects.get(email__iexact=email)
        if name:  # use the latest provided name
            person.name = name
    except Person.DoesNotExist:
        person = Person(name=name, email=email)

    return person


def find_date(mail):
    t = parsedate_tz(mail.get('Date', ''))
    if not t:
        return datetime.datetime.utcnow()
    return datetime.datetime.utcfromtimestamp(mktime_tz(t))


def find_headers(mail):
    headers = [(key, sanitise_header(value, header_name=key))
               for key, value in mail.items()]

    strings = [('%s: %s' % (key, header.encode()))
               for (key, header) in headers]

    return '\n'.join(strings)


def find_references(mail):
    """Construct a list of possible reply message ids."""
    refs = []

    if 'In-Reply-To' in mail:
        refs.append(mail.get('In-Reply-To'))

    if 'References' in mail:
        rs = mail.get('References').split()
        rs.reverse()
        for r in rs:
            if r not in refs:
                refs.append(r)

    return refs


def _find_matching_prefix(subject_prefixes, regex):
    for prefix in subject_prefixes:
        m = regex.match(prefix)
        if m:
            return m


def parse_series_marker(subject_prefixes):
    """Extract series markers from subject.

    Extract the markers of multi-patches series, i.e. 'x/n', from the
    provided subject series.

    Args:
        subject_prefixes: List of subject prefixes to extract markers
          from

    Returns:
        (x, n) if markers found, else (None, None)
    """

    regex = re.compile('^([0-9]+)/([0-9]+)$')
    m = _find_matching_prefix(subject_prefixes, regex)
    if m:
        return (int(m.group(1)), int(m.group(2)))

    return (None, None)


def parse_version(subject, subject_prefixes):
    """Extract patch version.

    Args:
        subject: Main body of subject line
        subject_prefixes: List of subject prefixes to extract version
          from

    Returns:
        version if found, else 1
    """
    regex = re.compile('^[vV](\d+)$')
    m = _find_matching_prefix(subject_prefixes, regex)
    if m:
        return int(m.group(1))

    m = re.search(r'\([vV](\d+)\)', subject)
    if m:
        return int(m.group(1))

    return 1


def find_content(mail):
    """Extract a comment and potential diff from a mail."""
    patchbuf = None
    commentbuf = ''

    for part in mail.walk():
        if part.get_content_maintype() != 'text':
            continue

        payload = part.get_payload(decode=True)
        subtype = part.get_content_subtype()

        if not isinstance(payload, six.text_type):
            charset = part.get_content_charset()

            # Check that we have a charset that we understand. Otherwise,
            # ignore it and fallback to our standard set.
            if charset is not None:
                try:
                    codecs.lookup(charset)
                except LookupError:
                    charset = None

            # If there is no charset or if it is unknown, then try some common
            # charsets before we fail.
            if charset is None:
                try_charsets = ['utf-8', 'windows-1252', 'iso-8859-1']
            else:
                try_charsets = [charset]

            for cset in try_charsets:
                try:
                    payload = six.text_type(payload, cset)
                    break
                except UnicodeDecodeError:
                    payload = None

            # Could not find a valid decoded payload.  Fail.
            if payload is None:
                return None, None

        if subtype in ['x-patch', 'x-diff']:
            patchbuf = payload
        elif subtype == 'plain':
            c = payload

            if not patchbuf:
                patchbuf, c = parse_patch(payload)

            if c is not None:
                commentbuf += c.strip() + '\n'

    commentbuf = clean_content(commentbuf)

    return patchbuf, commentbuf


def find_submission_for_comment(project, refs):
    for ref in refs:
        # first, check for a direct reply
        try:
            submission = Submission.objects.get(project=project, msgid=ref)
            return submission
        except Submission.DoesNotExist:
            pass

        # see if we have comments that refer to a patch
        try:
            comment = Comment.objects.get(submission__project=project,
                                          msgid=ref)
            return comment.submission
        except Comment.MultipleObjectsReturned:
            # NOTE(stephenfin): This is a artifact of prior lack of support
            # for cover letters in Patchwork. Previously all replies to
            # patches were saved as comments. However, it's possible that
            # someone could have created a new series as a reply to one of the
            # comments on the original patch series. For example,
            # '2015-November/002096.html' from the Patchwork archives. In this
            # case, reparsing the archives will result in creation of a cover
            # letter with the same message ID as the existing comment. Follow
            # up comments will then apply to both this cover letter and the
            # linked patch from the comment previously created. We choose to
            # apply the comment to the cover letter. Note that this only
            # happens when running 'parsearchive' or similar, so it should not
            # affect every day use in any way.
            comments = Comment.objects.filter(submission__project=project,
                                              msgid=ref)
            # The latter item will be the cover letter
            return comments.reverse()[0].submission
        except Comment.DoesNotExist:
            pass

    return None


def split_prefixes(prefix):
    """Turn a prefix string into a list of prefix tokens."""
    split_re = re.compile(r'[,\s]+')
    matches = split_re.split(prefix)

    return [s for s in matches if s != '']


def clean_subject(subject, drop_prefixes=None):
    """Clean a Subject: header from an incoming patch.

    Removes Re: and Fwd: strings, as well as [PATCH]-style prefixes. By
    default, only [PATCH] is removed, and we keep any other bracketed
    data in the subject. If drop_prefixes is provided, remove those
    too, comparing case-insensitively.

    Args:
        subject: Subject to be cleaned
        drop_prefixes: Additional, case-insensitive prefixes to remove
          from the subject
    """
    re_re = re.compile(r'^(re|fwd?)[:\s]\s*', re.I)
    prefix_re = re.compile(r'^\[([^\]]*)\]\s*(.*)$')
    subject = clean_header(subject)

    if drop_prefixes is None:
        drop_prefixes = []
    else:
        drop_prefixes = [s.lower() for s in drop_prefixes]

    drop_prefixes.append('patch')

    # remove Re:, Fwd:, etc
    subject = re_re.sub(' ', subject)

    subject = normalise_space(subject)

    prefixes = []

    match = prefix_re.match(subject)

    while match:
        prefix_str = match.group(1)
        prefixes += [p for p in split_prefixes(prefix_str)
                     if p.lower() not in drop_prefixes]

        subject = match.group(2)
        match = prefix_re.match(subject)

    subject = normalise_space(subject)

    subject = subject.strip()
    if prefixes:
        subject = '[%s] %s' % (','.join(prefixes), subject)

    return (subject, prefixes)


def subject_check(subject):
    """Determine if a mail is a reply."""
    comment_re = re.compile(r'^(re)[:\s]\s*', re.I)

    return comment_re.match(clean_header(subject))


def clean_content(content):
    """Remove cruft from the email message.

    Catch signature (-- ) and list footer (_____) cruft.
    """
    sig_re = re.compile(r'^(-- |_+)\n.*', re.S | re.M)
    content = sig_re.sub('', content)

    return content.strip()


def parse_patch(content):
    """Split a mail's contents into a diff and comment.

    This is a state machine that takes a patch, generally in UNIX mbox
    format, and splits it into the component comments and diff.

    Args:
        patch: The patch to be split

    Returns:
        A tuple containing the diff and comment. Either one or both of
        these can be empty.

    Raises:
        Exception: The state machine transitioned to an invalid state.
    """
    patchbuf = ''
    commentbuf = ''
    buf = ''

    # state specified the line we just saw, and what to expect next
    state = 0
    # 0: text
    # 1: suspected patch header (diff, ====, Index:)
    # 2: patch header line 1 (---)
    # 3: patch header line 2 (+++)
    # 4: patch hunk header line (@@ line)
    # 5: patch hunk content
    # 6: patch meta header (rename from/rename to)
    #
    # valid transitions:
    #  0 -> 1 (diff, ===, Index:)
    #  0 -> 2 (---)
    #  1 -> 2 (---)
    #  2 -> 3 (+++)
    #  3 -> 4 (@@ line)
    #  4 -> 5 (patch content)
    #  5 -> 1 (run out of lines from @@-specifed count)
    #  1 -> 6 (rename from / rename to)
    #  6 -> 2 (---)
    #  6 -> 1 (other text)
    #
    # Suspected patch header is stored into buf, and appended to
    # patchbuf if we find a following hunk. Otherwise, append to
    # comment after parsing.

    # line counts while parsing a patch hunk
    lc = (0, 0)
    hunk = 0

    for line in content.split('\n'):
        line += '\n'

        if state == 0:
            if line.startswith('diff ') or line.startswith('===') \
                    or line.startswith('Index: '):
                state = 1
                buf += line
            elif line.startswith('--- '):
                state = 2
                buf += line
            else:
                commentbuf += line
        elif state == 1:
            buf += line
            if line.startswith('--- '):
                state = 2

            if line.startswith(('rename from ', 'rename to ')):
                state = 6
        elif state == 2:
            if line.startswith('+++ '):
                state = 3
                buf += line
            elif hunk:
                state = 1
                buf += line
            else:
                state = 0
                commentbuf += buf + line
                buf = ''
        elif state == 3:
            match = _hunk_re.match(line)
            if match:
                def fn(x):
                    if not x:
                        return 1
                    return int(x)

                lc = [fn(x) for x in match.groups()]

                state = 4
                patchbuf += buf + line
                buf = ''
            elif line.startswith('--- '):
                patchbuf += buf + line
                buf = ''
                state = 2
            elif hunk and line.startswith(r'\ No newline at end of file'):
                # If we had a hunk and now we see this, it's part of the patch,
                # and we're still expecting another @@ line.
                patchbuf += line
            elif hunk:
                state = 1
                buf += line
            else:
                state = 0
                commentbuf += buf + line
                buf = ''
        elif state in [4, 5]:
            if line.startswith('-'):
                lc[0] -= 1
            elif line.startswith('+'):
                lc[1] -= 1
            elif line.startswith(r'\ No newline at end of file'):
                # Special case: Not included as part of the hunk's line count
                pass
            else:
                lc[0] -= 1
                lc[1] -= 1

            patchbuf += line

            if lc[0] <= 0 and lc[1] <= 0:
                state = 3
                hunk += 1
            else:
                state = 5
        elif state == 6:
            if line.startswith(('rename to ', 'rename from ')):
                patchbuf += buf + line
                buf = ''
            elif line.startswith('--- '):
                patchbuf += buf + line
                buf = ''
                state = 2
            else:
                buf += line
                state = 1
        else:
            raise Exception("Unknown state %d! (line '%s')" % (state, line))

    commentbuf += buf

    if patchbuf == '':
        patchbuf = None

    if commentbuf == '':
        commentbuf = None

    return patchbuf, commentbuf


def parse_pull_request(content):
    git_re = re.compile(r'^The following changes since commit.*' +
                        r'^are available in the git repository at:\n'
                        r'^\s*([\S]+://[^\n]+)$',
                        re.DOTALL | re.MULTILINE)
    match = git_re.search(content)
    if match:
        return match.group(1)
    return None


def find_state(mail):
    """Return the state with the given name or the default."""
    state_name = mail.get('X-Patchwork-State', '').strip()
    if state_name:
        try:
            return State.objects.get(name__iexact=state_name)
        except State.DoesNotExist:
            pass
    return get_default_initial_patch_state()


def find_delegate_by_filename(project, filenames):
    if not filenames:
        return None

    rules = list(DelegationRule.objects.filter(project=project))

    patch_delegate = None

    for filename in filenames:
        file_delegate = None
        for rule in rules:
            if fnmatch(filename, rule.path):
                file_delegate = rule.user
                break

        if file_delegate is None:
            return None

        if patch_delegate is not None and file_delegate != patch_delegate:
            return None

        patch_delegate = file_delegate

    return patch_delegate


def find_delegate_by_header(mail):
    """Return the delegate with the given email or None."""
    delegate_email = mail.get('X-Patchwork-Delegate', '').strip()
    if delegate_email:
        try:
            return User.objects.get(email__iexact=delegate_email)
        except User.DoesNotExist:
            pass
    return None


def parse_mail(mail, list_id=None):
    """Parse a mail and add to the database.

    Args:
        mail (`mbox.Mail`): Mail to parse and add.
        list_id (str): Mailing list ID

    Returns:
        None
    """
    # some basic sanity checks
    if 'From' not in mail:
        raise ValueError("Missing 'From' header")

    if 'Subject' not in mail:
        raise ValueError("Missing 'Subject' header")

    if 'Message-Id' not in mail:
        raise ValueError("Missing 'Message-Id' header")

    hint = mail.get('X-Patchwork-Hint', '').lower()
    if hint == 'ignore':
        logger.debug("Ignoring email due to 'ignore' hint")
        return

    if list_id:
        project = find_project_by_id(list_id)
    else:
        project = find_project_by_header(mail)

    if project is None:
        logger.error('Failed to find a project for email')
        return

    # parse content

    diff, message = find_content(mail)

    if not (diff or message):
        return  # nothing to work with

    msgid = mail.get('Message-Id').strip()
    author = find_author(mail)
    subject = mail.get('Subject')
    name, prefixes = clean_subject(subject, [project.linkname])
    is_comment = subject_check(subject)
    x, n = parse_series_marker(prefixes)
    version = parse_version(name, prefixes)
    refs = find_references(mail)
    date = find_date(mail)
    headers = find_headers(mail)
    pull_url = parse_pull_request(message)

    # build objects

    if not is_comment and (diff or pull_url):  # patches or pull requests
        # we delay the saving until we know we have a patch.
        author.save()

        delegate = find_delegate_by_header(mail)
        if not delegate and diff:
            filenames = find_filenames(diff)
            delegate = find_delegate_by_filename(project, filenames)

        series = find_series(mail)
        # We will create a new series if:
        # - we have a patch number (x of n), and
        # - either:
        #    * there is no series, or
        #    * the version doesn't match
        #    * we have a patch with this number already
        if n and ((not series) or
                  (series.version != version) or
                  (SeriesPatch.objects.filter(series=series, number=x).count()
                   )):
            series = Series(date=date,
                            submitter=author,
                            version=version,
                            total=n)
            series.save()

            # NOTE(stephenfin) We must save references for series. We
            # do this to handle the case where a later patch is
            # received first. Without storing references, it would not
            # be possible to identify the relationship between patches
            # as the earlier patch does not reference the later one.
            for ref in refs + [msgid]:
                # we don't want duplicates
                try:
                    # we could have a ref to a previous series. (For
                    # example, a series sent in reply to another
                    # series.) That should not create a series ref
                    # for this series, so check for the msg-id only,
                    # not the msg-id/series pair.
                    SeriesReference.objects.get(msgid=ref)
                except SeriesReference.DoesNotExist:
                    SeriesReference.objects.create(series=series, msgid=ref)

        patch = Patch(
            msgid=msgid,
            project=project,
            name=name,
            date=date,
            headers=headers,
            submitter=author,
            content=message,
            diff=diff,
            pull_url=pull_url,
            delegate=delegate,
            state=find_state(mail))
        patch.save()
        logger.debug('Patch saved')

        # add to a series if we have found one, and we have a numbered
        # patch. Don't add unnumbered patches (for example diffs sent
        # in reply, or just messages with random refs/in-reply-tos)
        if series and x:
            series.add_patch(patch, x)

        return patch
    elif x == 0:  # (potential) cover letters
        # if refs are empty, it's implicitly a cover letter. If not,
        # however, we need to see if a match already exists and, if
        # not, assume that it is indeed a new cover letter
        is_cover_letter = False
        if not is_comment:
            if not refs == []:
                try:
                    CoverLetter.objects.all().get(name=name)
                except CoverLetter.DoesNotExist:
                    # if no match, this is a new cover letter
                    is_cover_letter = True
                except CoverLetter.MultipleObjectsReturned:
                    # if multiple cover letters are found, just ignore
                    pass
            else:
                is_cover_letter = True

        if is_cover_letter:
            author.save()

            # we don't use 'find_series' here as a cover letter will
            # always be the first item in a thread, thus the references
            # could only point to a different series or unrelated
            # message
            try:
                series = SeriesReference.objects.get(msgid=msgid).series
            except SeriesReference.DoesNotExist:
                series = None

            if not series:
                series = Series(date=date,
                                submitter=author,
                                version=version,
                                total=n)
                series.save()

                # we don't save the in-reply-to or references fields
                # for a cover letter, as they can't refer to the same
                # series
                SeriesReference.objects.get_or_create(series=series,
                                                      msgid=msgid)

            cover_letter = CoverLetter(
                msgid=msgid,
                project=project,
                name=name,
                date=date,
                headers=headers,
                submitter=author,
                content=message)
            cover_letter.save()
            logger.debug('Cover letter saved')

            series.add_cover_letter(cover_letter)

            return cover_letter

    # comments

    # we only save comments if we have the parent email
    submission = find_submission_for_comment(project, refs)
    if not submission:
        return

    if is_comment and diff:
        message += diff

    author.save()

    comment = Comment(
        submission=submission,
        msgid=msgid,
        date=date,
        headers=headers,
        submitter=author,
        content=message)
    comment.save()
    logger.debug('Comment saved')

    return comment


def find_filenames(diff):
    """Find files changes in a given diff."""
    # normalise spaces
    diff = diff.replace('\r', '')
    diff = diff.strip() + '\n'

    filenames = {}

    for line in diff.split('\n'):
        if len(line) <= 0:
            continue

        filename_match = _filename_re.match(line)
        if not filename_match:
            continue

        filename = filename_match.group(2)
        if filename.startswith('/dev/null'):
            continue

        filename = '/'.join(filename.split('/')[1:])
        filenames[filename] = True

    filenames = sorted(filenames.keys())

    return filenames
