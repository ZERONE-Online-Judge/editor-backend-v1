"""Resource selection shared by local and queued Editor compilation."""


def compile_resources(resources, language, is_solution):
    selected=[]
    for resource in resources:
        policy=resource.get('resource_policy')
        if policy:
            if is_solution and language in policy['languages'] and 'SOLUTION' in policy['targets'] and 'COMPILE' in policy['stages']:
                selected.append(resource)
        elif resource['kind']=='resource' or (resource['kind']=='aux' and resource.get('attach_to_build') is not False) or (is_solution and resource.get('compile_with_solution')):
            selected.append(resource)
    return selected
