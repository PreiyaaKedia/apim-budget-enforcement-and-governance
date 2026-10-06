targetScope = 'resourceGroup'

@description('Short lowercase prefix used in globally unique resource names.')
@minLength(3)
@maxLength(20)
param namePrefix string

@description('Container image containing the cost enforcement API. Leave empty to use the image repository and tag in the registry created by this template.')
param containerImage string = ''

@description('Repository name used for the remotely built image.')
param imageRepository string = 'cost-enforcement-api'

@description('Tag used for the remotely built image.')
param imageTag string = 'python3-fix'

@description('Container image containing the budget console. Leave empty to use the console repository and tag in the registry created by this template.')
param consoleContainerImage string = ''

@description('Repository name used for the remotely built console image.')
param consoleImageRepository string = 'budget-console'

@description('Tag used for the remotely built console image.')
param consoleImageTag string = 'latest'

@description('Deploy the Container App. Set to false for the initial registry bootstrap, then remotely build the image and redeploy with true.')
param deployContainerApp bool = true

@description('Deploy the budget console. Set to false during the initial registry bootstrap.')
param deployConsole bool = true

@description('Entra tenant that issues APIM managed identity tokens.')
param tenantId string = tenant().tenantId

@description('Audience exposed by the cost API. Use the service principal name registered in Entra, such as the bare application ID.')
param audience string

@description('Client ID of the APIM managed identity allowed to call the API.')
param apimManagedIdentityClientId string

@description('Client ID of the Entra SPA used by the console MSAL Browser authorization-code flow with PKCE.')
param consoleClientId string

@description('Employee email domains allowed to self-provision as console members.')
param consoleAllowedEmailDomains array

@description('Username for the local console owner account.')
param consoleOwnerUsername string

@secure()
@description('PBKDF2-SHA256 password hash for the local console owner account.')
param consoleOwnerPasswordHash string

@secure()
@description('Random secret used to sign console owner and employee sessions.')
param consoleOwnerSessionSecret string

@description('OAuth scope requested by the console managed identity when calling the cost API.')
param costApiScope string = 'api://${audience}/.default'

@description('Full resource ID of the Application Insights component queried by the FinOps dashboard.')
param applicationInsightsResourceId string

@description('Allow unauthenticated access to admin budget endpoints for prototype testing. Keep false outside controlled testing.')
param adminAuthDisabled bool = false

@description('Monthly caller budget in decimal USD.')
param monthlyLimitUsd string = '100.00'

@description('Name of the cost API Container App.')
param costApiName string = '${namePrefix}-vnet-api'

@description('Name of the budget console Container App.')
param consoleAppName string = '${namePrefix}-budget-console'

@description('Name of the Container Apps environment.')
param containerAppsEnvironmentName string = '${namePrefix}-vnet-env'

@description('Name of the virtual network.')
param virtualNetworkName string = '${namePrefix}-cost-vnet'

@description('Optional globally unique Container Registry name. Leave empty to derive one from the deployment scope.')
param containerRegistryName string = ''

@description('Optional globally unique Cosmos DB account name. Leave empty to derive one from the deployment scope.')
param cosmosAccountName string = ''

@description('Address space for the cost enforcement virtual network.')
param vnetAddressPrefix string = '10.20.0.0/16'

@description('Dedicated subnet for the workload-profile Container Apps environment.')
param containerAppsSubnetPrefix string = '10.20.0.0/27'

@description('Dedicated subnet for private endpoints.')
param privateEndpointSubnetPrefix string = '10.20.1.0/24'

param location string = resourceGroup().location

var uniqueSuffix = uniqueString(subscription().subscriptionId, resourceGroup().id)
var cosmosName = empty(cosmosAccountName) ? take('${namePrefix}-${uniqueSuffix}', 44) : cosmosAccountName
var registryName = empty(containerRegistryName) ? toLower(take('${replace(namePrefix, '-', '')}${uniqueSuffix}', 50)) : containerRegistryName
var vnetName = virtualNetworkName
var containerAppsSubnetName = 'container-apps-infrastructure'
var privateEndpointSubnetName = 'private-endpoints'
var environmentName = containerAppsEnvironmentName
var appName = costApiName
var pullIdentityName = '${namePrefix}-pull'
var cosmosPrivateEndpointName = '${namePrefix}-cosmos-pe'
var cosmosPrivateDnsZoneName = 'privatelink.documents.azure.com'
var acrPullRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var effectiveContainerImage = empty(containerImage) ? '${registry.properties.loginServer}/${imageRepository}:${imageTag}' : containerImage
var effectiveConsoleImage = empty(consoleContainerImage) ? '${registry.properties.loginServer}/${consoleImageRepository}:${consoleImageTag}' : consoleContainerImage
var applicationInsightsIdParts = split(applicationInsightsResourceId, '/')
var applicationInsightsSubscriptionId = applicationInsightsIdParts[2]
var applicationInsightsResourceGroup = applicationInsightsIdParts[4]
var applicationInsightsName = applicationInsightsIdParts[8]

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: containerAppsSubnetName
        properties: {
          addressPrefix: containerAppsSubnetPrefix
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: privateEndpointSubnetName
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource containerAppsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: containerAppsSubnetName
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: privateEndpointSubnetName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource pullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: pullIdentityName
  location: location
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, pullIdentity.id, acrPullRoleDefinitionId)
  scope: registry
  properties: {
    principalId: pullIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleDefinitionId
  }
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: cosmosName
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    ipRules: []
    minimalTlsVersion: 'Tls12'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: 'cost-enforcement'
  properties: {
    resource: {
      id: 'cost-enforcement'
    }
  }
}

resource ledger 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: 'ledger'
  properties: {
    resource: {
      id: 'ledger'
      partitionKey: {
        paths: [
          '/partitionKey'
        ]
        kind: 'Hash'
        version: 2
      }
      conflictResolutionPolicy: {
        mode: 'LastWriterWins'
        conflictResolutionPath: '/_ts'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          {
            path: '/*'
          }
        ]
        excludedPaths: [
          {
            path: '/usage/*'
          }
          {
            path: '/"_etag"/?'
          }
        ]
      }
    }
  }
}

resource pricing 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: 'pricing'
  properties: {
    resource: {
      id: 'pricing'
      partitionKey: {
        paths: [
          '/deployment'
        ]
        kind: 'Hash'
        version: 2
      }
      conflictResolutionPolicy: {
        mode: 'LastWriterWins'
        conflictResolutionPath: '/_ts'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          {
            path: '/*'
          }
        ]
        excludedPaths: [
          {
            path: '/"_etag"/?'
          }
        ]
      }
    }
  }
}

resource cosmosPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: cosmosPrivateDnsZoneName
  location: 'global'
}

resource cosmosPrivateDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: cosmosPrivateDnsZone
  name: '${namePrefix}-cost-vnet-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource cosmosPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: cosmosPrivateEndpointName
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: '${cosmosPrivateEndpointName}-connection'
        properties: {
          privateLinkServiceId: cosmos.id
          groupIds: [
            'Sql'
          ]
          requestMessage: 'Private access for the cost enforcement API.'
        }
      }
    ]
  }
}

resource cosmosPrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: cosmosPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'cosmos-sql'
        properties: {
          privateDnsZoneId: cosmosPrivateDnsZone.id
        }
      }
    ]
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: containerAppsSubnet.id
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource console 'Microsoft.App/containerApps@2024-03-01' = if (deployConsole) {
  name: consoleAppName
  location: location
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${pullIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: [
        {
          name: 'console-owner-password-hash'
          value: consoleOwnerPasswordHash
        }
        {
          name: 'console-owner-session-secret'
          value: consoleOwnerSessionSecret
        }
      ]
      registries: [
        {
          server: registry.properties.loginServer
          identity: pullIdentity.id
        }
      ]
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
    }
    template: {
      containers: [
        {
          name: 'console'
          image: effectiveConsoleImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            { name: 'COST_ENFORCEMENT_URL', value: 'https://${appName}.${environment.properties.defaultDomain}' }
            { name: 'COST_ENFORCEMENT_SCOPE', value: costApiScope }
            { name: 'APPLICATION_INSIGHTS_RESOURCE_ID', value: applicationInsightsResourceId }
            { name: 'CONSOLE_AUTH_DISABLED', value: 'false' }
            { name: 'CONSOLE_ALLOWED_EMAIL_DOMAINS', value: join(consoleAllowedEmailDomains, ',') }
            { name: 'CONSOLE_OWNER_USERNAME', value: consoleOwnerUsername }
            { name: 'CONSOLE_OWNER_PASSWORD_HASH', secretRef: 'console-owner-password-hash' }
            { name: 'CONSOLE_OWNER_SESSION_SECRET', secretRef: 'console-owner-session-secret' }
            { name: 'CONSOLE_OWNER_COOKIE_SECURE', value: 'true' }
            { name: 'CONSOLE_ENTRA_CLIENT_ID', value: consoleClientId }
            { name: 'CONSOLE_ENTRA_TENANT_ID', value: tenantId }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 3
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '25'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    acrPull
  ]
}

resource consoleAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = if (deployConsole) {
  parent: console
  name: 'current'
  properties: {
    platform: {
      enabled: false
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployContainerApp) {
  name: appName
  location: location
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${pullIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: registry.properties.loginServer
          identity: pullIdentity.id
        }
      ]
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
    }
    template: {
      containers: [
        {
          name: 'api'
          image: effectiveContainerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
            { name: 'COSMOS_DATABASE', value: database.name }
            { name: 'COSMOS_LEDGER_CONTAINER', value: ledger.name }
            { name: 'COSMOS_PRICING_CONTAINER', value: pricing.name }
            { name: 'MONTHLY_LIMIT_USD', value: monthlyLimitUsd }
            { name: 'TENANT_ID', value: tenantId }
            { name: 'AUDIENCE', value: audience }
            { name: 'ALLOWED_CLIENT_ID', value: apimManagedIdentityClientId }
            { name: 'ADMIN_ALLOWED_PRINCIPAL_ID', value: console.?identity.principalId ?? '' }
            { name: 'ADMIN_REQUIRED_ROLE', value: 'CostEnforcement.Admin' }
            { name: 'ADMIN_AUTH_DISABLED', value: string(adminAuthDisabled) }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 10
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    acrPull
    cosmosPrivateDnsZoneGroup
    cosmosPrivateDnsVnetLink
  ]
}

module consoleMonitoringReader 'monitoring-reader.bicep' = if (deployConsole) {
  name: 'console-monitoring-reader'
  scope: resourceGroup(applicationInsightsSubscriptionId, applicationInsightsResourceGroup)
  params: {
    applicationInsightsName: applicationInsightsName
    principalId: console.?identity.principalId ?? ''
  }
}

resource cosmosDataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = if (deployContainerApp) {
  parent: cosmos
  name: guid(cosmos.id, app.id, 'cosmos-data-contributor')
  properties: {
    principalId: app.?identity.principalId ?? ''
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: cosmos.id
  }
}

output apiUrl string = deployContainerApp ? 'https://${app.?properties.configuration.ingress.fqdn ?? ''}' : ''
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output defaultContainerImage string = '${registry.properties.loginServer}/${imageRepository}:${imageTag}'
output defaultConsoleImage string = '${registry.properties.loginServer}/${consoleImageRepository}:${consoleImageTag}'
output cosmosAccountName string = cosmos.name
output containerAppPrincipalId string = app.?identity.principalId ?? ''
output consoleUrl string = deployConsole ? 'https://${console.?properties.configuration.ingress.fqdn ?? ''}' : ''
output consolePrincipalId string = console.?identity.principalId ?? ''
