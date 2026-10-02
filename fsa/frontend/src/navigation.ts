export function isNavigationActive(pathname: string, href: string): boolean {
  if (href === '/') return pathname === '/' || pathname.startsWith('/imports/');
  return pathname === href || pathname.startsWith(`${href}/`);
}
