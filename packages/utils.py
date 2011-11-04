from operator import itemgetter

from django.db import connection
from django.db.models import Count, Max
from django.contrib.auth.models import User

from main.models import Package, Repo
from main.utils import cache_function, groupby_preserve_order, PackageStandin
from .models import (PackageGroup, PackageRelation,
        SignoffSpecification, Signoff, DEFAULT_SIGNOFF_SPEC)

@cache_function(300)
def get_group_info(include_arches=None):
    raw_groups = PackageGroup.objects.values_list(
            'name', 'pkg__arch__name').order_by('name').annotate(
             cnt=Count('pkg'), last_update=Max('pkg__last_update'))
    # now for post_processing. we need to seperate things out and add
    # the count in for 'any' to all of the other architectures.
    group_mapping = {}
    for grp in raw_groups:
        arch_groups = group_mapping.setdefault(grp[1], {})
        arch_groups[grp[0]] = {'name': grp[0], 'arch': grp[1],
                'count': grp[2], 'last_update': grp[3]}

    # we want to promote the count of 'any' packages in groups to the
    # other architectures, and also add any 'any'-only groups
    if 'any' in group_mapping:
        any_groups = group_mapping['any']
        del group_mapping['any']
        for arch, arch_groups in group_mapping.iteritems():
            for grp in any_groups.itervalues():
                if grp['name'] in arch_groups:
                    found = arch_groups[grp['name']]
                    found['count'] += grp['count']
                    if grp['last_update'] > found['last_update']:
                        found['last_update'] = grp['last_update']
                else:
                    new_g = grp.copy()
                    # override the arch to not be 'any'
                    new_g['arch'] = arch
                    arch_groups[grp['name']] = new_g

    # now transform it back into a sorted list, including only the specified
    # architectures if we got a list
    groups = []
    for key, val in group_mapping.iteritems():
        if not include_arches or key in include_arches:
            groups.extend(val.itervalues())
    return sorted(groups, key=itemgetter('name', 'arch'))

class Difference(object):
    def __init__(self, pkgname, repo, pkg_a, pkg_b):
        self.pkgname = pkgname
        self.repo = repo
        self.pkg_a = pkg_a
        self.pkg_b = pkg_b

    def classes(self):
        '''A list of CSS classes that should be applied to this row in any
        generated HTML. Useful for sorting, filtering, etc. Contains whether
        this difference is in both architectures or the sole architecture it
        belongs to, as well as the repo name.'''
        css_classes = [self.repo.name.lower()]
        if self.pkg_a and self.pkg_b:
            css_classes.append('both')
        elif self.pkg_a:
            css_classes.append(self.pkg_a.arch.name)
        elif self.pkg_b:
            css_classes.append(self.pkg_b.arch.name)
        return ' '.join(css_classes)

    def __cmp__(self, other):
        if isinstance(other, Difference):
            return cmp(self.__dict__, other.__dict__)
        return False

@cache_function(300)
def get_differences_info(arch_a, arch_b):
    # This is a monster. Join packages against itself, looking for packages in
    # our non-'any' architectures only, and not having a corresponding package
    # entry in the other table (or having one with a different pkgver). We will
    # then go and fetch all of these packages from the database and display
    # them later using normal ORM models.
    sql = """
SELECT p.id, q.id
    FROM packages p
    LEFT JOIN packages q
    ON (
        p.pkgname = q.pkgname
        AND p.repo_id = q.repo_id
        AND p.arch_id != q.arch_id
        AND p.id != q.id
    )
    WHERE p.arch_id IN (%s, %s)
    AND (
        q.id IS NULL
        OR p.pkgver != q.pkgver
        OR p.pkgrel != q.pkgrel
        OR p.epoch != q.epoch
    )
"""
    cursor = connection.cursor()
    cursor.execute(sql, [arch_a.id, arch_b.id])
    results = cursor.fetchall()
    # column A will always have a value, column B might be NULL
    to_fetch = [row[0] for row in results]
    # fetch all of the necessary packages
    pkgs = Package.objects.normal().in_bulk(to_fetch)
    # now build a list of tuples containing differences
    differences = []
    for row in results:
        pkg_a = pkgs.get(row[0])
        pkg_b = pkgs.get(row[1])
        # We want arch_a to always appear first
        # pkg_a should never be None
        if pkg_a.arch == arch_a:
            item = Difference(pkg_a.pkgname, pkg_a.repo, pkg_a, pkg_b)
        else:
            # pkg_b can be None in this case, so be careful
            name = pkg_a.pkgname if pkg_a else pkg_b.pkgname
            repo = pkg_a.repo if pkg_a else pkg_b.repo
            item = Difference(name, repo, pkg_b, pkg_a)
        if item not in differences:
            differences.append(item)

    # now sort our list by repository, package name
    differences.sort(key=lambda a: (a.repo.name, a.pkgname))
    return differences

def get_wrong_permissions():
    sql = """
SELECT DISTINCT id
    FROM (
        SELECT pr.id, p.repo_id, pr.user_id
        FROM packages p
        JOIN packages_packagerelation pr ON p.pkgbase = pr.pkgbase
        WHERE pr.type = %s
        ) pkgs
    WHERE pkgs.repo_id NOT IN (
        SELECT repo_id FROM user_profiles_allowed_repos ar
        INNER JOIN user_profiles up ON ar.userprofile_id = up.id
        WHERE up.user_id = pkgs.user_id
    )
"""
    cursor = connection.cursor()
    cursor.execute(sql, [PackageRelation.MAINTAINER])
    to_fetch = [row[0] for row in cursor.fetchall()]
    relations = PackageRelation.objects.select_related('user').filter(
            id__in=to_fetch)
    return relations


def approved_by_signoffs(signoffs, spec):
    if signoffs:
        good_signoffs = sum(1 for s in signoffs if not s.revoked)
        return good_signoffs >= spec.required
    return False

class PackageSignoffGroup(object):
    '''Encompasses all packages in testing with the same pkgbase.'''
    def __init__(self, packages):
        if len(packages) == 0:
            raise Exception
        self.packages = packages
        self.user = None
        self.target_repo = None
        self.signoffs = set()

        first = packages[0]
        self.pkgbase = first.pkgbase
        self.arch = first.arch
        self.repo = first.repo
        self.version = ''
        self.last_update = first.last_update
        self.packager = first.packager
        self.maintainers = User.objects.filter(
                package_relations__type=PackageRelation.MAINTAINER,
                package_relations__pkgbase=self.pkgbase)

        self.specification = \
                SignoffSpecification.objects.get_or_default_from_package(first)
        self.default_spec = self.specification is DEFAULT_SIGNOFF_SPEC

        version = first.full_version
        if all(version == pkg.full_version for pkg in packages):
            self.version = version

    @property
    def package(self):
        '''Try and return a relevant single package object representing this
        group. Start by seeing if there is only one package, then look for the
        matching package by name, finally falling back to a standin package
        object.'''
        if len(self.packages) == 1:
            return self.packages[0]

        same_pkgs = [p for p in self.packages if p.pkgname == p.pkgbase]
        if same_pkgs:
            return same_pkgs[0]

        return PackageStandin(self.packages[0])

    def find_signoffs(self, all_signoffs):
        '''Look through a list of Signoff objects for ones matching this
        particular group and store them on the object.'''
        for s in all_signoffs:
            if s.pkgbase != self.pkgbase:
                continue
            if self.version and not s.full_version == self.version:
                continue
            if s.arch_id == self.arch.id and s.repo_id == self.repo.id:
                self.signoffs.add(s)

    def approved(self):
        return approved_by_signoffs(self.signoffs, self.specification)

    @property
    def completed(self):
        return sum(1 for s in self.signoffs if not s.revoked)

    @property
    def required(self):
        return self.specification.required

    def user_signed_off(self, user=None):
        '''Did a given user signoff on this package? user can be passed as an
        argument, or attached to the group object itself so this can be called
        from a template.'''
        if user is None:
            user = self.user
        return user in (s.user for s in self.signoffs if not s.revoked)

    def __unicode__(self):
        return u'%s-%s (%s): %d' % (
                self.pkgbase, self.version, self.arch, len(self.signoffs))

def get_current_signoffs(repos):
    '''Returns a mapping of pkgbase -> signoff objects for the given repos.'''
    cursor = connection.cursor()
    sql = """
SELECT DISTINCT s.id
    FROM packages_signoff s
    JOIN packages p ON (
        s.pkgbase = p.pkgbase
        AND s.pkgver = p.pkgver
        AND s.pkgrel = p.pkgrel
        AND s.epoch = p.epoch
        AND s.arch_id = p.arch_id
        AND s.repo_id = p.repo_id
    )
    WHERE p.repo_id IN (
"""
    sql += ", ".join("%s" for r in repos)
    sql += ")"
    cursor.execute(sql, [r.id for r in repos])

    results = cursor.fetchall()
    # fetch all of the returned signoffs by ID
    to_fetch = [row[0] for row in results]
    signoffs = Signoff.objects.select_related('user').in_bulk(to_fetch)
    return signoffs.values()

def get_target_repo_map(pkgbases):
    package_repos = Package.objects.order_by().values_list(
            'pkgbase', 'repo__name').filter(
            pkgbase__in=pkgbases).distinct()
    return dict(package_repos)

def get_signoff_groups(repos=None):
    if repos is None:
        repos = Repo.objects.filter(testing=True)
    repo_ids = [r.pk for r in repos]

    test_pkgs = Package.objects.select_related(
            'arch', 'repo', 'packager').filter(repo__in=repo_ids)
    packages = test_pkgs.order_by('pkgname')

    # Collect all pkgbase values in testing repos
    q_pkgbase = test_pkgs.values('pkgbase')
    pkgtorepo = get_target_repo_map(q_pkgbase)

    # Collect all existing signoffs for these packages
    signoffs = get_current_signoffs(repos)

    same_pkgbase_key = lambda x: (x.repo.name, x.arch.name, x.pkgbase)
    grouped = groupby_preserve_order(packages, same_pkgbase_key)
    signoff_groups = []
    for group in grouped:
        signoff_group = PackageSignoffGroup(group)
        signoff_group.target_repo = pkgtorepo.get(signoff_group.pkgbase,
                "Unknown")
        signoff_group.find_signoffs(signoffs)
        signoff_groups.append(signoff_group)

    return signoff_groups

# vim: set ts=4 sw=4 et:
