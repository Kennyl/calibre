#!/usr/bin/env python2
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

from __future__ import (unicode_literals, division, absolute_import,
                        print_function)
from functools import partial
import operator

from cssutils.css import Property
import regex

from calibre import force_unicode
from calibre.ebooks import parse_css_length
from calibre.ebooks.oeb.normalize_css import normalizers, safe_parser

REGEX_FLAGS = regex.VERSION1 | regex.UNICODE | regex.IGNORECASE

def compile_pat(pat):
    return regex.compile(pat, flags=REGEX_FLAGS)

def all_properties(decl):
    ' This is needed because CSSStyleDeclaration.getProperties(None, all=True) does not work and is slower than it needs to be. '
    for item in decl.seq:
        p = item.value
        if isinstance(p, Property):
            yield p

class StyleDeclaration(object):

    def __init__(self, css_declaration):
        self.css_declaration = css_declaration
        self.expanded_properties = {}
        self.changed = False

    def __iter__(self):
        dec = self.css_declaration
        for p in all_properties(dec):
            if isinstance(p, Property):
                n = normalizers.get(p.name)
                if n is None:
                    yield p, None
                else:
                    if p not in self.expanded_properties:
                        self.expanded_properties[p] = [Property(k, v, p.literalpriority) for k, v in n(p.name, p.propertyValue).iteritems()]
                    for ep in self.expanded_properties[p]:
                        yield ep, p

    def expand_property(self, parent_prop):
        props = self.expanded_properties.pop(parent_prop, None)
        if props is None:
            return
        dec = self.css_declaration
        seq = dec._tempSeq()
        for item in dec.seq:
            if item.value is parent_prop:
                for c in props:
                    c.parent = dec
                    seq.append(c, 'Property')
            else:
                seq.appendItem(item)
        dec._setSeq(seq)

    def remove_property(self, prop, parent_prop):
        if parent_prop is not None:
            self.expand_property(parent_prop)
        dec = self.css_declaration
        seq = dec._tempSeq()
        for item in dec.seq:
            if item.value is not prop:
                seq.appendItem(item)
        dec._setSeq(seq)
        self.changed = True

    def change_property(self, prop, parent_prop, val):
        if parent_prop is not None:
            self.expand_property(parent_prop)
        prop.value = val
        self.changed = True

    def append_properties(self, props):
        if props:
            self.changed = True
            for prop in props:
                self.css_declaration.setProperty(Property(prop.name, prop.value, prop.literalpriority, self.css_declaration))

    def __str__(self):
        return force_unicode(self.css_declaration.cssText, 'utf-8')

operator_map = {'==':'eq', '<=':'le', '<':'lt', '>=':'ge', '>':'gt', '-':'sub', '+': 'add', '*':'mul', '/':'truediv'}

def unit_convert(value, unit, dpi=96.0, body_font_size=12):
    result = None
    if unit == 'px':
        result = value * 72.0 / dpi
    elif unit == 'in':
        result = value * 72.0
    elif unit == 'pt':
        result = value
    elif unit == 'pc':
        result = value * 12.0
    elif unit == 'mm':
        result = value * 2.8346456693
    elif unit == 'cm':
        result = value * 28.346456693
    elif unit == 'rem':
        result = value * body_font_size
    elif unit == 'q':
        result = value * 0.708661417325
    return result

def parse_css_length_or_number(raw, default_unit='px'):
    if isinstance(raw, (int, long, float)):
        return raw, default_unit
    try:
        return float(raw), default_unit
    except Exception:
        return parse_css_length(raw)

def numeric_match(value, unit, pts, op, raw):
    try:
        v, u = parse_css_length_or_number(raw)
    except Exception:
        return False
    if v is None:
        return False
    if unit is None or u is None or unit == u:
        return op(v, value)
    if pts is None:
        return False
    p = unit_convert(v, u)
    if p is None:
        return False
    return op(p, pts)

def transform_number(val, op, raw):
    try:
        v, u = parse_css_length_or_number(raw)
    except Exception:
        return raw
    if v is None:
        return raw
    v = op(v, val)
    if int(v) == v:
        v = int(v)
    return str(v) + u

class Rule(object):

    def __init__(self, property='color', match_type='*', query='', action='remove', action_data=''):
        self.property_name = property.lower()
        self.action, self.action_data = action, action_data
        if self.action == 'append':
            decl = safe_parser().parseStyle(self.action_data)
            self.appended_properties = list(all_properties(decl))
        elif self.action in '+-/*':
            self.action_operator = partial(transform_number, float(self.action_data), getattr(operator, operator_map[self.action]))
        if match_type == 'is':
            self.property_matches = lambda x: x.lower() == query
        elif match_type == '*':
            self.property_matches = lambda x: True
        elif 'matches' in match_type:
            q = compile_pat(query)
            if match_type.startswith('not_'):
                self.property_matches = lambda x: q.match(x) is None
            else:
                self.property_matches = lambda x: q.match(x) is not None
        else:
            value, unit = parse_css_length_or_number(query, default_unit=None)
            op = getattr(operator, operator_map[match_type])
            pts = unit_convert(value, unit)
            self.property_matches = partial(numeric_match, value, unit, pts, op)

    def process_declaration(self, declaration):
        oval, declaration.changed = declaration.changed, False
        for prop, parent_prop in declaration:
            if prop.name == self.property_name and self.property_matches(prop.value):
                if self.action == 'remove':
                    declaration.remove_property(prop, parent_prop)
                elif self.action == 'change':
                    declaration.change_property(prop, parent_prop, self.action_data)
                elif self.action == 'append':
                    declaration.append_properties(self.appended_properties)
                else:
                    val = prop.value
                    nval = self.action_operator(val)
                    if val != nval:
                        declaration.change_property(prop, parent_prop, nval)
        changed = declaration.changed
        declaration.changed = oval or changed
        return changed

def test():  # {{{
    import unittest

    class TestTransforms(unittest.TestCase):
        longMessage = True
        maxDiff = None
        ae = unittest.TestCase.assertEqual

        def test_matching(self):

            def apply_rule(style, **rule):
                r = Rule(**rule)
                decl = StyleDeclaration(safe_parser().parseStyle(style))
                r.process_declaration(decl)
                return str(decl)

            def m(match_type='*', query=''):
                self.ae(ecss, apply_rule(css, property=prop, match_type=match_type, query=query))

            prop = 'color'
            css, ecss = 'color: red; margin: 0', 'margin: 0'
            m('*')
            m('is', 'red')
            m('matches', 'R.d')
            m('not_matches', 'blue')
            ecss = css.replace('; ', ';\n')
            m('is', 'blue')

            prop = 'margin-top'
            css, ecss = 'color: red; margin-top: 10', 'color: red'
            m('*')
            m('==', '10')
            m('<=', '10')
            m('>=', '10')
            m('<', '11')
            m('>', '9')
            css, ecss = 'color: red; margin-top: 1mm', 'color: red'
            m('==', '1')
            m('==', '1mm')
            m('==', '4q')
            ecss = css.replace('; ', ';\n')
            m('==', '1pt')

    tests = unittest.defaultTestLoader.loadTestsFromTestCase(TestTransforms)
    unittest.TextTestRunner(verbosity=4).run(tests)

if __name__ == '__main__':
    test()
# }}}